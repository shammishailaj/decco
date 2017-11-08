# Copyright (c) Platform9 systems. All rights reserved

# pylint: disable=dangerous-default-value,unused-variable,too-many-locals
# pylint: disable=too-many-arguments

import time
import logging
import base64
import MySQLdb
from kubernetes import client, config
from kubernetes.client.models.v1_secret import V1Secret
from kubernetes.client.models.v1_delete_options import V1DeleteOptions
from kubernetes.client.models.extensions_v1beta1_deployment import ExtensionsV1beta1Deployment
from kubernetes.client.models.extensions_v1beta1_deployment_spec import ExtensionsV1beta1DeploymentSpec
from kubernetes.client.models.v1_object_meta import V1ObjectMeta
from kubernetes.client.models.v1_pod_template_spec import V1PodTemplateSpec
from kubernetes.client.models.v1_container import V1Container
from kubernetes.client.models.v1_env_var import V1EnvVar
from kubernetes.client.models.v1_pod_spec import V1PodSpec
from tempfile import mkdtemp
from os import path
from decco_tests.utils.decco_api import DeccoApi
from setupd.config import Configuration, CertificateData
from setupd.fts import create_and_verify_db_connection, ensure_metadata_schema
from string import Template
from contextlib import contextmanager
from subprocess import Popen

LOG = logging.getLogger(__name__)

import pf9lab.hosts.authorize as labrole
from pf9lab.retry import retry
from pf9lab.testbeds.common import generate_short_du_name
from pf9lab.hosts.authorize import typical_fabric_settings
from pf9lab.du.auth import login
from pf9deploy.server.util.passwords import generate_random_password
from pf9deploy.server.secrets import SecretsManager
from pf9lab.testbeds import Testbed
# from qbert_tests.testbeds import aws_utils as qbaws
from fabric.api import sudo, put, get
from StringIO import StringIO
import re
import os
from os.path import dirname, join as pjoin
from subprocess import check_call, Popen, PIPE
import requests
import json


# CSPI_MISC_DIR = pjoin(dirname(decco_tests.__file__), 'misc')
CSPI_MISC_DIR = ''
AWS_REGION = os.getenv('AWS_REGION', 'us-west-1')
CONTAINER_IMAGES_FILE = os.getenv('CONTAINER_IMAGES_FILE')
config.load_kube_config()


@retry(log=LOG, max_wait=60)
def retried_login(*largs, **kwargs):
    return login(*largs, **kwargs)


def new_configuration(db, admin_user, shortname, state_fqdn, region):
    cfg = Configuration()
    cfg.customer.fullname = 'decco test customer'
    cfg.customer.admin_user = admin_user
    cfg.customer.shortname = shortname
    cfg.fqdn = state_fqdn
    cfg.region = region
    cfg.release_version = 'platform9-decco-1.0.0'
    cfg.save(db)
    cfg.sync_certificates()
    cfg.sync_passwords()
    cfg.save(db)
    return cfg


def checked_local_call(cmd):
    p = Popen(cmd, stdout=PIPE)
    p.wait()
    if p.returncode != 0:
        raise Exception('command %s returned %d' % (' '.join(cmd), p.returncode))
    return p.stdout.read()

def generate_setupd_valid_password():
    """
    setupd requires that passwords contain at least one digit, one uppercase
    letter and one lowercase letter. pf9deploy's generate_random_password
    can sometimes violate this, so check it before using it.
    If we can't do it in less than 100 iterations, something is really wrong,
    so fail.
    FIXME: This code is pretty much copied from pf9_setup.py. We should pull
    it into a third place where it can be used by both - maybe in firkinize.
    """
    validation_regexes = [
        re.compile(r'[0-9]'),
        re.compile(r'[a-z]'),
        re.compile(r'[A-Z]')
    ]
    def _valid_password(passwd):
        if len(passwd) < 10:
            return False

        for pwd_rgx in validation_regexes:
            if not pwd_rgx.search(passwd):
                return False
        return True

    tries = 0
    while tries < 100:
        tries += 1
        passwd = generate_random_password()
        if _valid_password(passwd):
            LOG.info('Generated good password in %d attempt(s)', tries)
            return passwd
    raise RuntimeError('Failed to generate setupd acceptable password!')

def pull_container_image(host_info, image_id_or_name):
    dp_stdout, dp_stderr = checked_sudo(host_info['ip'], 'docker pull %s' % image_id_or_name)
    # TODO: return image sha?


def run_container_image(host_info, image_id_or_tag,
                        network=None, detached=True,
                        port_mappings=dict(),
                        env_vars=dict(),
                        volumes=dict(),
                        cmd=None):
    """
    Runs the image in the container

    :type host_info: dict
    :param host_info: see `pf9lab.hosts.provider.provider_pf9.HostProvider.make_testbed`
    :type image_id_or_tag: str
    :param image_id_or_tag: the source image id or repository name:tag
    :type network: str
    :param network: if specified, the name of the docker network to run the container in
    :type detached: bool
    :param detached: if True, run the new container in the background
    :type port_mappings: dict
    :param port_mappings: map of ports to publish: {host port: container port}
    :type env_vars: dict
    :param env_vars: map of environment variable names to values to set in container
                     runtime
    :type volumes: dict
    :param volumes: map of volumes to mount: {host path: container path}
    :type cmd: str
    :param cmd: alternative command to run rather than the image default

    :return: the container id
    """
    cmd_parts = ['docker', 'run']
    if network:
        cmd_parts += ['--network', network]
    if detached:
        cmd_parts.append('-d')
    for host_port, container_port in port_mappings.iteritems():
        cmd_parts += ['-p', '%d:%d' % (host_port, container_port)]
    for host_path, container_path in volumes.iteritems():
        cmd_parts += ['-v', '%s:%s' % (host_path, container_path)]
    for env_name, env_val in env_vars.iteritems():
        cmd_parts += ['-e', '"%s=%s"' % (env_name, env_val)]
    cmd_parts.append(image_id_or_tag)
    if cmd:
        cmd_parts.append(cmd)
    dr_stdout, _ = checked_sudo(host_info['ip'], ' '.join(cmd_parts))
    container_sha = dr_stdout.strip()
    return container_sha


def install_and_run_consul_container(host_info):
    pull_container_image(host_info, 'consul')
    run_container_image(host_info, 'consul',
                            network='host',
                            port_mappings={8085: 8085})


def ecr_login(host_info):
    docker_login_cmd = checked_local_call(['aws', '--region', AWS_REGION,
                                           'ecr', 'get-login', '--no-include-email'])
    if not docker_login_cmd:
        raise Exception('get-login did not return docker login command')
    if not docker_login_cmd.startswith('docker login'):
        raise Exception('weird output from get-login: %s' % docker_login_cmd)

    # checked_sudo(host_info['ip'], docker_login_cmd)


def consul_set_recursive(endpoint, kv_tree, position_stack=list()):
    for kv_k, kv_v in kv_tree.iteritems():
        if type(kv_v) == dict:
            LOG.debug('recursing into %s', kv_k)
            consul_set_recursive(endpoint, kv_v, position_stack + [kv_k])
        else:
            uri = '/'.join(position_stack + [kv_k])
            LOG.info('PUT %s/%s', endpoint, uri)
            if type(kv_v) not in (str, unicode):
                kv_v = json.dumps(kv_v)
            resp = requests.put(endpoint + '/' + uri, data=kv_v)
            LOG.info('%s', str(resp))


def add_customize_env_vars(du, user, password, shortname):
    """
    We don't use ansible customization, but the base RawKubTestbed expects the
    DU dictionary to contain 'customer_env_vars' containing the DU username
    password etc. Add it here...
    """
    env_vars = {
        'ADMINUSER': user,
        'ADMINPASS': password,
        'CUSTOMER_SHORTNAME': shortname,
        'CUSTOMER_FULLNAME': shortname
    }
    du['customize_env_vars'] = env_vars

def setup_decco_hosts(du_address, hosts, admin_user, admin_password, token):
    """
    Install hostagent on all the hosts, then enable and wait for the qbert
    role. Adds the resmgr host id to the each host's dictionary if hostagent
    is installer successfully.
    """
    if not hosts:
        LOG.info('No kube hosts to setup')
        return

    for host in hosts:
        labrole.install_certless_hostagent(du_address,
                                           host['ip'],
                                           admin_user,
                                           admin_password,
                                           'service')
    for host in hosts:
        host_info = labrole.wait_unauthed_role(du_address,
                                               token,
                                               host['hostname'],
                                               'pf9-kube')
        host['host_id'] = host_info['id']
        labrole.authorize_role(du_address, host['host_id'], 'pf9-kube', token)

    for host in hosts:
        labrole.wait_for_role(du_address, host['host_id'], 'pf9-kube', token)


def start_mysql(namespace):
    root_passwd = generate_setupd_valid_password()
    spec = {
        'initialReplicas': 1,
        'verifyTcpClientCert': True,
        'container': {
            'name': 'mysql',
            'image': 'mysql',
            'env': [
                {
                    'name': 'MYSQL_ROOT_PASSWORD',
                    'value': root_passwd
                }
            ],
            'ports': [
                {
                    'containerPort': 3306,
                }
            ]
        }
    }

    dapi = DeccoApi()
    for i in range(5):
        try:
            time.sleep(2)
            dapi.create_app('mysql', spec, namespace)
            LOG.info('successfully created mysql app')
            return root_passwd
        except:
            LOG.info("failed to create mysql app, may retry...")
    raise Exception('failed to create mysql app')


def create_http_wildcard_cert_secret(secret_name, domain):
    sm = SecretsManager()
    cert_entry = sm.db.certs.find_one({'type': 'wildcard', 'domain': domain})
    certdata = sm.get_secret(cert_entry['tags']['cert'])
    certdata = base64.b64encode(certdata)
    keydata = sm.get_secret(cert_entry['tags']['key'])
    keydata = base64.b64encode(keydata)
    v1 = client.CoreV1Api()
    secret = V1Secret(metadata={'name': secret_name})
    secret.data = {
        'tls.crt': certdata,
        'tls.key': keydata
    }
    v1.create_namespaced_secret('decco', secret)


def create_tcp_wildcard_ca_and_cert(customer_shortname, customer_fqdn):
    ca = CertificateData.generate_ca(cn=customer_shortname,
                                     du_id=0,
                                     set_version=0)
    tcp_wildcard_cn = '*.%s' % customer_fqdn
    tcp_cert = CertificateData.generate_certificate(tcp_wildcard_cn, ca)
    return ca, tcp_cert


def create_tcp_wildcard_cert_secret(secret_name, ca, tcp_cert):
    ca_cert_base64 = base64.b64encode(ca.cert_pem)
    tcp_cert_base64 = base64.b64encode(tcp_cert.cert_pem)
    tcp_key_base64 = base64.b64encode(tcp_cert.private_key_pem)
    secret = V1Secret(metadata={'name': secret_name})
    secret.data = {
        'ca.pem': ca_cert_base64,
        'key.pem': tcp_key_base64,
        'cert.pem': tcp_cert_base64
    }
    v1 = client.CoreV1Api()
    v1.create_namespaced_secret('decco', secret)


def read_global_tcp_certs():
    v1 = client.CoreV1Api()
    s = v1.read_namespaced_secret('tcp-cert-global', 'global')
    client_key = base64.b64decode(s.data['key.pem'])
    client_cert = base64.b64decode(s.data['cert.pem'])
    ca_cert = base64.b64decode(s.data['ca.pem'])
    return ca_cert, client_cert, client_key


def generate_stunnel_config(fqdn, svc_name, svc_port,
                            ca_cert, client_cert, client_key):
    tmp_dir = mkdtemp(fqdn)
    with open(path.join(tmp_dir, 'ca.pem'), 'w') as f:
        f.write(ca_cert)
    with open(path.join(tmp_dir, 'cert.pem'), 'w') as f:
        f.write(client_cert)
    with open(path.join(tmp_dir, 'key.pem'), 'w') as f:
        f.write(client_key)
    template = """
socket=l:TCP_NODELAY=1
socket=r:TCP_NODELAY=1

debug=7
# output=/dev/stdout
foreground=yes

[app]
client=yes
accept=${svc_port}
connect=${fqdn}:443
sni=${svc_name}.${fqdn}
# checkHost = ${svc_name}.${fqdn}
cert=${tmp_dir}/cert.pem
key=${tmp_dir}/key.pem
verifyChain=yes
CAfile=${tmp_dir}/ca.pem
"""
    tmpl = Template(template=template)
    conf = tmpl.substitute({}, fqdn=fqdn, svc_name=svc_name,
                           svc_port=svc_port, tmp_dir=tmp_dir)
    stunnel_conf_path = path.join(tmp_dir, 'stunnel.conf')
    with open(stunnel_conf_path, 'w') as f:
        f.write(conf)
    return tmp_dir, stunnel_conf_path

@contextmanager
def stunnel(stunnel_conf_path, output_dir):
    stunnel_path = os.getenv('STUNNEL_PATH')
    if not stunnel_path:
        raise Exception('STUNNEL_PATH not defined')
    output_path = path.join(output_dir, 'stunnel.log')
    with open(output_path, 'w') as f:
        popen = Popen([stunnel_path, stunnel_conf_path], stdout=f, stderr=f)
        LOG.info('popen process pid: %s and log: %s', popen.pid, output_path)
        try:
            yield popen
        finally:
            popen.terminate()


def _get_db_connection(mysql_root_passwd):
    for attempt in range(5):
        try:
            time.sleep(10)
            db = create_and_verify_db_connection('127.0.0.1', 3306,
                                                 'root',
                                                 mysql_root_passwd)
            return db
        except Exception as ex:
            LOG.info('failed to connect to db, may retry in a bit (%s)', ex)
    raise Exception('failed to connect to db')

class DeccoTestbed(Testbed):
    """
    testbed with no DU, rather 1 host that sort of acts like one.
    Has rabbitmq and consul (via container) installed.
    """

    def __init__(self, tag, kube_config_base64, global_region_info):
        # self.hosts = []
        super(DeccoTestbed, self).__init__()
        self.kube_config_base64 = kube_config_base64
        self.tag = tag
        self.global_region_info = global_region_info


    @classmethod
    def create(cls, tag):

        # Note that the only compatible (image, flavor) combinations are
        # centos7-latest, ubuntu16 and ubuntu16, with 1cpu.2gb.40gb, at least
        # that I know of as of 9/19/17 -Bob
        kubeConfigPath = os.getenv('KUBECONFIG')
        if kubeConfigPath is None:
            raise Exception('KUBECONFIG not defined')
        with open(kubeConfigPath, "r") as file:
            data = file.read()
            kube_config_base64 = base64.b64encode(data)

        if False:
            global_tcp_ca_cert, global_tcp_client_cert, global_tcp_client_key = \
                read_global_tcp_certs()

            tmp_dir, stunnel_conf_path = generate_stunnel_config(
                'global.platform9.horse', 'consul', 8500, global_tcp_ca_cert,
                global_tcp_client_cert, global_tcp_client_key)

            with stunnel(stunnel_conf_path, tmp_dir):
                LOG.info('stunnel to consul running')

        #aws_access_key = os.getenv('AWS_ACCESS_KEY')
        #aws_secret_key = os.getenv('AWS_SECRET_KEY')
        #if not aws_access_key or not aws_secret_key:
        #    raise Exception('AWS credentials are required to pull from ECR')

        image_tag = os.getenv('IMAGE_TAG', 'latest')
        registry_url = os.getenv('REGISTRY_URL')
        if not registry_url:
            raise Exception('Where are we pulling containers from?')

        # install container image/tag list
        #if CONTAINER_IMAGES_FILE:
        #    if not os.path.isfile(CONTAINER_IMAGES_FILE):
        #        LOG.warning('images file set to %s but does not exist?',
        #                CONTAINER_IMAGES_FILE)
        #    else:
        #        with open(CONTAINER_IMAGES_FILE, 'r') as f:
        #            LOG.info(yaml.load(f.read()))
        #        with typical_fabric_settings(controller['ip']):
        #            put(CONTAINER_IMAGES_FILE, '/etc/setupd.images.in')

        LOG.info('image tag: %s', image_tag)

        customer_shortname = os.getenv('CUSTOMER_SHORTNAME')
        if not customer_shortname:
            customer_shortname = generate_short_du_name(tag)

        admin_user = 'whoever@example.com'
        admin_password = generate_setupd_valid_password()

        domain = 'platform9.horse'
        customer_fqdn = '%s.%s' % (customer_shortname, domain)
        region_name = 'RegionOne'
        region_fqdn = '%s-%s.%s' % (customer_shortname, region_name, domain)

        ca, tcp_cert = create_tcp_wildcard_ca_and_cert(customer_shortname,
                                                       customer_fqdn)
        tcp_cert_secret_name = 'tcp-cert-%s' % customer_shortname
        create_tcp_wildcard_cert_secret(tcp_cert_secret_name, ca, tcp_cert)

        http_cert_secret_name = 'http-cert-%s' % customer_shortname
        create_http_wildcard_cert_secret(http_cert_secret_name, domain)
        dapi = DeccoApi()
        global_region_spec = {
            'domainName': domain,
            'httpCertSecretName': http_cert_secret_name,
            'tcpCertAndCaSecretName': tcp_cert_secret_name
        }
        dapi.create_cust_region(customer_shortname, global_region_spec)
        mysql_root_passwd = start_mysql(customer_shortname)

        tmp_dir, stunnel_conf_path = generate_stunnel_config(
            customer_fqdn, 'mysql', 3306,
            ca.cert_pem, tcp_cert.cert_pem, tcp_cert.private_key_pem)
        LOG.info('stunnel conf path: %s' % stunnel_conf_path)
        with stunnel(stunnel_conf_path, tmp_dir):
            LOG.info('stunnel started')
            db = _get_db_connection(mysql_root_passwd)
            with db.cursor() as cursor:
                cursor.execute('CREATE DATABASE IF NOT EXISTS `pf9_metadata`')
            db.commit()
            db.select_db('pf9_metadata')
            ensure_metadata_schema(db)
            cfg = new_configuration(db, admin_user, customer_shortname,
                                    customer_fqdn, region_name)
            LOG.info('db setup complete')

        global_region_info = {
            'name': customer_shortname,
            'mysql_root_passwd': mysql_root_passwd,
            'spec': global_region_spec
        }

        # LOG.info('Adding %s to route53 for %s...',
        #          customer_fqdn, controller['ip'])
        # qbaws.create_dns_record([controller['ip']], customer_fqdn)

        # webcert, webkey = put_wildcard_keypair(controller['ip'], domain)


        LOG.info('waiting for keystone to become open')
        #sleep(5)

        LOG.info('obtaining token')
        # user-watch might need a few seconds to propagate the initial admin user
        token = 'dummy_token'
        if not token:
            token_info = retried_login('https://%s' % customer_fqdn,
                                       'whoever@example.com', admin_password,
                                       'service')
            token = token_info['access']['token']['id']
            tenant_id = token_info['access']['token']['tenant']['id']
            LOG.info('token: %s', str(token_info))

        #setup_decco_hosts(controller['ip'], kube_hosts, admin_user,
        #                 admin_password, token)

        return cls(tag, kube_config_base64, global_region_info)

    @staticmethod
    def from_dict(desc):
        """ desc is a dict """
        type_name = '.'.join([__name__, DeccoTestbed.__name__])
        if desc['type'] != type_name:
            raise ValueError('attempt to build %s with %s' %
                             (type_name, desc['type']))
        return DeccoTestbed(desc['tag'],
                            desc['kube_config_base64'],
                            desc['global_region_info']
                            )

    def to_dict(self):
        return {
            'type': '.'.join([__name__, DeccoTestbed.__name__]),
            'kube_config_base64': self.kube_config_base64,
            'global_region_info': self.global_region_info,
            'tag': self.tag
        }

    def destroy(self):
        LOG.info('Destroying decco testbed')
        dapi = DeccoApi()
        try:
            dapi.delete_cust_region(self.global_region_info['name'])
        except:
            LOG.exception("warning: failed to delete customer region")
        global_region_spec = self.global_region_info['spec']
        v1 = client.CoreV1Api()
        for key in ['httpCertSecretName', 'tcpCertAndCaSecretName']:
            try:
                secret_name = global_region_spec[key]
                v1.delete_namespaced_secret(secret_name, 'decco',
                                            V1DeleteOptions())
            except:
                LOG.exception("warning: failed to delete secret")
