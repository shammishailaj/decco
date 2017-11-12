
from kubernetes.client import ApiClient
from kubernetes.client.configuration import Configuration


class DeccoApi():
    def __init__(self):
        config = Configuration()
        if not config.api_client:
            config.api_client = ApiClient()
        self.api_client = config.api_client

    def list_spaces(self, ns='decco'):
        prefix = '/apis/decco.platform9.com/v1beta2/namespaces'
        resource_path = '%s/%s/spaces' % (prefix, ns)
        collection_formats = {}
        path_params = {}
        query_params = {}
        header_params = {}
        form_params = []
        local_var_files = {}
        body_params = None
        # HTTP header `Accept`
        header_params['Accept'] = \
            self.api_client.select_header_accept(['application/json',
                                                  'application/yaml'])
        header_params['Content-Type'] = \
            self.api_client.select_header_content_type(['*/*'])

        # Authentication setting
        auth_settings = ['BearerToken']


        data = self.api_client.call_api(resource_path, 'GET',
                                        path_params,
                                        query_params,
                                        header_params,
                                        body=body_params,
                                        post_params=form_params,
                                        files=local_var_files,
                                        response_type='object',
                                        auth_settings=auth_settings,
                                        _return_http_data_only=True,
                                        collection_formats=collection_formats)
        return data

    def delete_space(self, name, ns='decco'):
        prefix = '/apis/decco.platform9.com/v1beta2/namespaces'
        resource_path = '%s/%s/spaces/%s' % (prefix, ns, name)
        path_params = {}
        query_params = {}
        header_params = {}
        # HTTP header `Accept`
        header_params['Accept'] = \
            self.api_client.select_header_accept(['application/json',
                                                  'application/yaml'])
        header_params['Content-Type'] = \
            self.api_client.select_header_content_type(['*/*'])

        # Authentication setting
        auth_settings = ['BearerToken']
        data = self.api_client.call_api(resource_path, 'DELETE',
                                        path_params,
                                        query_params,
                                        header_params,
                                        auth_settings=auth_settings)
        return data

    def create_space(self, name, spec, ns='decco'):
        return self._create_resource('Space', 'spaces', name, spec, ns)

    def create_app(self, name, spec, ns):
        return self._create_resource('App', 'apps', name, spec, ns)

    def _create_resource(self, kind, plural_kind, name, spec, ns):
        prefix = '/apis/decco.platform9.com/v1beta2/namespaces'
        resource_path = '%s/%s/%s' % (prefix, ns, plural_kind)
        collection_formats = {}
        path_params = {}
        query_params = {}
        header_params = {}
        form_params = []
        local_var_files = {}
        body_params = {
            'metadata': {
                'name': name
            },
            'apiVersion': 'decco.platform9.com/v1beta2',
            'kind': kind,
            'spec': spec
        }
        # HTTP header `Accept`
        header_params['Accept'] = \
            self.api_client.select_header_accept(['application/json',
                                                  'application/yaml'])
        header_params['Content-Type'] = \
            self.api_client.select_header_content_type(['*/*'])

        # Authentication setting
        auth_settings = ['BearerToken']
        data = self.api_client.call_api(resource_path, 'POST',
                                        path_params,
                                        query_params,
                                        header_params,
                                        body=body_params,
                                        post_params=form_params,
                                        files=local_var_files,
                                        response_type='object',
                                        auth_settings=auth_settings,
                                        _return_http_data_only=True,
                                        collection_formats=collection_formats)
        return data