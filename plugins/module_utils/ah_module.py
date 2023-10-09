from __future__ import absolute_import, division, print_function

__metaclass__ = type

from ansible.module_utils.basic import AnsibleModule, env_fallback
from ansible.module_utils.urls import (
    Request,
    SSLValidationError,
    ConnectionError,
    fetch_file,
)
from ansible.module_utils.six import string_types
from ansible.module_utils.six import PY2, PY3
from ansible.module_utils.six.moves.urllib.parse import urlparse, urlencode
from ansible.module_utils.six.moves.urllib.error import HTTPError
from ansible.module_utils.six.moves.http_cookiejar import CookieJar
from ansible.module_utils._text import to_bytes, to_native, to_text
import os.path
from socket import gethostbyname
import re
from json import loads, dumps
import base64
import os
import time
import email.mime.multipart
import email.mime.application


class ItemNotDefined(Exception):
    pass

class AHModuleError(Exception):
    """API request error exception.

    :param error_message: Error message.
    :type error_message: str
    """

    def __init__(self, error_message):
        """Initialize the object."""
        self.error_message = error_message

    def __str__(self):
        """Return the error message."""
        return self.error_message


class AHModule(AnsibleModule):
    url = None
    session = None
    AUTH_ARGSPEC = dict(
        ah_host=dict(
            required=False,
            aliases=["ah_hostname"],
            fallback=(env_fallback, ["AH_HOST"]),
        ),
        ah_username=dict(required=False, fallback=(env_fallback, ["AH_USERNAME"])),
        ah_password=dict(no_log=True, required=False, fallback=(env_fallback, ["AH_PASSWORD"])),
        ah_path_prefix=dict(
            required=False,
            default="galaxy",
            fallback=(env_fallback, ["GALAXY_API_PATH_PREFIX"]),
        ),
        validate_certs=dict(
            type="bool",
            aliases=["ah_verify_ssl"],
            required=False,
            fallback=(env_fallback, ["AH_VERIFY_SSL"]),
        ),
        ah_token=dict(
            type="raw",
            no_log=True,
            required=False,
            fallback=(env_fallback, ["AH_API_TOKEN"]),
        ),
        ah_sso_token=dict(
            no_log=True,
            required=False,
            fallback=(env_fallback, ["AH_SSO_TOKEN"]),
        ),
        ah_cookie=dict(
            no_log=True,
            required=False,
            fallback=(env_fallback, ["AH_API_COOKIE"]),
        ),
        ah_auth_url=dict(
            required=False,
            fallback=(env_fallback, ["AH_AUTH_URL"]),
        ),
        client_id=dict(
            required=False,
            fallback=(env_fallback, ["AH_CLIENT_ID"]),
        ),
        request_timeout=dict(
            type="float",
            required=False,
            fallback=(env_fallback, ["AH_REQUEST_TIMEOUT"])
        ),
    )
    ENCRYPTED_STRING = "$encrypted$"
    short_params = {
        "host": "ah_host",
        "username": "ah_username",
        "password": "ah_password",
        "path_prefix": "ah_path_prefix",
        "verify_ssl": "validate_certs",
        "oauth_token": "ah_token",
        "sso_token": "ah_sso_token",
        "auth_cookie": "ah_cookie",
        "auth_url": "ah_auth_url",
        "client_id": "client_id",
        "request_timeout": "request_timeout",
    }
    IDENTITY_FIELDS = {}
    ENCRYPTED_STRING = "$encrypted$"
    host = "127.0.0.1"
    username = None
    password = None
    path_prefix = "galaxy"
    verify_ssl = True
    auth_url = None
    oauth_token = None
    sso_token = None
    auth_cookie = None
    client_id= "automation-hub" # For Red Hat SSO use 'cloud-services', Keycloak galaxy-ng its 'galaxy-ng'
    request_timeout = 10
    basic_auth = False
    authenticated = False
    error_callback = None
    warn_callback = None

    def __init__(self, argument_spec=None, direct_params=None, error_callback=None, warn_callback=None, require_auth=True, **kwargs):
        full_argspec = {}
        if require_auth:
            full_argspec.update(AHModule.AUTH_ARGSPEC)
        full_argspec.update(argument_spec)
        kwargs["supports_check_mode"] = True

        self.error_callback = error_callback
        self.warn_callback = warn_callback

        self.json_output = {"changed": False}

        if direct_params is not None:
            self.params = direct_params

        super(AHModule, self).__init__(argument_spec=full_argspec, **kwargs)

        self.session = Request(cookies=CookieJar(), validate_certs=self.verify_ssl, timeout=self.request_timeout)

        # Parameters specified on command line will override settings in any config
        for short_param, long_param in self.short_params.items():
            direct_value = self.params.get(long_param)
            if direct_value is not None:
                setattr(self, short_param, direct_value)

        # Perform magic depending on whether ah_token is a string or a dict
        if self.params.get("ah_token"):
            token_param = self.params.get("ah_token")
            if type(token_param) is dict:
                if "token" in token_param:
                    self.oauth_token = self.params.get("ah_token")["token"]
                else:
                    self.fail_json(msg="The provided dict in ah_token did not properly contain the token entry")
            elif isinstance(token_param, string_types):
                self.oauth_token = self.params.get("ah_token")
            else:
                error_msg = "The provided ah_token type was not valid ({0}). Valid options are str or dict.".format(type(token_param).__name__)
                self.fail_json(msg=error_msg)

        # Start Session
        # self.headers = {
        #     "referer": self.host,
        #     "Content-Type": "application/json",
        #     "Accept": "application/json",
        # }

        # Perform some validations on urls
        self.host_url = urlparse(self.validate_url(self.host))
        if self.auth_url is not None:
            self.authorized_url = urlparse(self.validate_url(self.auth_url))

        if "update_secrets" in self.params:
            self.update_secrets = self.params.pop("update_secrets")
        else:
            self.update_secrets = True

        # Set the various path prefixes
        self.galaxy_path_prefix = "/api/{prefix}".format(prefix=self.path_prefix.strip("/"))
        self.ui_path_prefix = "{galaxy_prefix}/_ui/v1".format(galaxy_prefix=self.galaxy_path_prefix)
        self.plugin_path_prefix = "{galaxy_prefix}/v3/plugin".format(galaxy_prefix=self.galaxy_path_prefix)

        # Open a Session
        self.session = Request(validate_certs=self.verify_ssl, follow_redirects=True, timeout=self.request_timeout)
        self.authenticate()
        self.server_version = self.get_server_version()

    def validate_url(self, url):
        # Perform some basic validation
        if re.match("^http{0,1}://", url):
            validated_url = url
        elif not re.match("^https{0,1}://", url):
            validated_url = "https://{0}".format(url)
        elif re.match("^https{0,1}://", url):
            validated_url = url
        else:
            self.fail_json(msg="Unable to parse host protocol as a URL ({1}): {0}".format(url, e))

        # Try to parse the hostname as a url
        try:
            parsed_validated_url = urlparse(validated_url)
        except Exception as e:
            self.fail_json(msg="Unable to parse host as a URL ({1}): {0}".format(parsed_validated_url, e))
        return validated_url

    def authenticate(self):
        """Authenticate with the API."""
        # curl -k -i  -X GET -H "Accept: application/json" -H "Content-Type: application/json" https://hub.lab.example.com/api/galaxy/_ui/v1/auth/login/

        # HTTP/1.1 204 No Content
        # Server: nginx/1.18.0
        # Date: Tue, 10 Aug 2021 07:33:37 GMT
        # Content-Length: 0
        # Connection: keep-alive
        # Vary: Accept, Cookie
        # Allow: GET, POST, HEAD, OPTIONS
        # X-Frame-Options: SAMEORIGIN
        # Set-Cookie: csrftoken=jvdb...kKHo; expires=Tue, 09 Aug 2022 07:33:37 GMT; Max-Age=31449600; Path=/; SameSite=Lax
        # Strict-Transport-Security: max-age=15768000

        url = self.build_ui_url("auth/login")
        try:
            response = self.make_request_raw_reponse("GET", url)
        except AHModuleError as e:
            self.fail_json(msg="Unable to reach url:{url} error: {error}".format(error=e, url=url))
        # Set-Cookie: csrftoken=jvdb...kKHo; expires=Tue, 09 Aug 2022 07:33:37 GMT
        # for h in response.getheaders():
        #     if h[0].lower() == "set-cookie":
        #         k, v = h[1].split("=", 1)
        #         if k.lower() == "csrftoken":
        #             header = {"X-CSRFToken": v.split(";", 1)[0]}
        #             self.session.headers.update(header)
        #             break
        # self.fail_json(msg="head: {error}".format(error=response.getheaders()))
        # curl -k -i -X POST  -H 'referer: https://hub.lab.example.com' -H "Accept: application/json" -H "Content-Type: application/json"
        #      -H 'X-CSRFToken: jvdb...kKHo' --cookie 'csrftoken=jvdb...kKHo' -d '{"username":"admin","password":"redhat"}'
        #      https://hub.lab.example.com/api/galaxy/_ui/v1/auth/login/

        # HTTP/1.1 204 No Content
        # Server: nginx/1.18.0
        # Date: Tue, 10 Aug 2021 07:35:33 GMT
        # Content-Length: 0
        # Connection: keep-alive
        # Vary: Accept, Cookie
        # Allow: GET, POST, HEAD, OPTIONS
        # X-Frame-Options: SAMEORIGIN
        # Set-Cookie: csrftoken=6DVP...at9a; expires=Tue, 09 Aug 2022 07:35:33 GMT; Max-Age=31449600; Path=/; SameSite=Lax
        # Set-Cookie: sessionid=87b0iw12wyvy0353rk5fwci0loy5s615; expires=Tue, 24 Aug 2021 07:35:33 GMT; HttpOnly; Max-Age=1209600; Path=/; SameSite=Lax
        # Strict-Transport-Security: max-age=15768000
        ## Add cookie if provided
        # if self.auth_cookie: # If we can ever get cookies to work
        #     try:
        #         header = {"X-CSRFToken": self.auth_cookie}
        #         response = self.make_request_raw_reponse("GET", url)
        #         self.authenticated = True
        #     except AHModuleError as e:
        #         self.fail_json(msg="Authentication with cookie error: {error}".format(error=e))
        if self.sso_token:  # NOT working when passing in curl cookie
            # https://github.com/ansible/ansible/blob/8a5ccc9d63ab528b579c14c4519c70c6838c7d6c/lib/ansible/galaxy/token.py#L44
            # https://access.redhat.com/documentation/en-us/red_hat_process_automation_manager/7.4/html/integrating_red_hat_process_automation_manager_with_red_hat_single_sign-on/sso-third-party-proc_integrate-sso
            # https://access.redhat.com/documentation/en-us/red_hat_single_sign-on/7.4/html/server_administration_guide/sso_protocols#authorization_code_flow
            # https://access.redhat.com/solutions/3234871
            # https://github.com/ansible/ansible/pull/75601
            try:
                # Refresh the token we have
                # data={
                # 'grant_type': 'refresh_token',
                # 'client_id': self.client_id,
                # 'refresh_token': self.sso_token,
                # }
                # data = 'grant_type=refresh_token&client_id=%s&refresh_token=%s' % (self.client_id, self.sso_token)
                # header = {"Content-Type":  'application/x-www-form-urlencoded'}
                # response = self.make_request_raw_reponse("POST", self.authorized_url, headers=header, data=data)
                # Now test it and set it
                header = {"Authorization": "Bearer {0}".format(self.sso_token)}
                response = self.make_request_raw_reponse("GET", self._build_url(self.galaxy_path_prefix))
                #self.update_cookie(response)
                self.session.headers.update(header)
                self.authenticated = True
            except AHModuleError as e:
                self.fail_json(msg="Authentication with cookie error: {error}".format(error=e))
        elif self.oauth_token:  # Tested
            try:
                header = {"Authorization": "Token {0}".format(self.oauth_token)}
                #self.session.headers.update(header)
                self.make_request_raw_reponse("GET", url)
                self.session.headers.update(header)
                #self.fail_json(msg="header: {header}".format(header=response.getheaders()))
                #self.update_cookie(response)
                #self.fail_json(msg="header: {header}".format(header=self.session.headers))
                self.authenticated = True
                #self.fail_json(msg="{error}".format(error=response.getheaders()))
            except AHModuleError as e:
                self.fail_json(msg="Authentication with cookie error: {error}".format(error=e))
        elif self.username and self.password: # Tested
            try:
                basic_str = base64.b64encode("{0}:{1}".format(self.username, self.password).encode("ascii"))
                header = {"Authorization": "Basic {0}".format(basic_str.decode("ascii"))}
                self.make_request_raw_reponse("GET", url)
                self.session.headers.update(header)
            except AHModuleError as e:
                self.fail_json(msg="Authentication error: {error}".format(error=e))
            self.authenticated = True

    def update_cookie(self, response):
        for h in response.getheaders():
            if h[0].lower() == "set-cookie":
                k, v = h[1].split("=", 1)
                if k.lower() == "csrftoken":
                    header = {"X-CSRFToken": v.split(";", 1)[0]}
                    self.session.headers.update(header)
                    break

    def get_server_version(self):
        """Return the automation hub/galaxy server version.

        :return: the server version ("4.2.5" for example) or an empty string if
                 that information is not available.
        :rtype: str
        """
        url = self._build_url(self.galaxy_path_prefix)
        try:
            response = self.make_request("GET", url)
        except AHModuleError as e:
            self.fail_json(msg="Error while getting server version: {error}".format(error=e))
        if response["status_code"] != 200:
            error_msg = self.extract_error_msg(response)
            if error_msg:
                fail_msg = "Unable to get server version: {code}: {error}".format(code=response["status_code"], error=error_msg)
            else:
                fail_msg = "Unable to get server version: {code}".format(code=response["status_code"])
            self.fail_json(msg=fail_msg)
        return response["json"]["server_version"].replace('dev', '') if "server_version" in response["json"] else ""

    def build_url(self, endpoint, query_params=None):
        # Make sure we start with /api/vX
        if not endpoint.startswith("/"):
            endpoint = "/{0}".format(endpoint)
        if not endpoint.startswith("/api/"):
            endpoint = "api/{0}/v3{1}".format(self.path_prefix, endpoint)
        if not endpoint.endswith("/") and "?" not in endpoint:
            endpoint = "{0}/".format(endpoint)

        # Update the URL path with the endpoint
        url = self.host_url._replace(path=endpoint)

        if query_params:
            url = url._replace(query=urlencode(query_params))

        return url

    def _build_url(self, prefix, endpoint=None, query_params=None):
        """Return a URL from the given prefix and endpoint.

        The URL is build as follows::

            https://<host>/<prefix>/[<endpoint>]/[?<query>]

        :param prefix: Prefix to add to the endpoint.
        :type prefix: str
        :param endpoint: Usually the API object name ("users", "groups", ...)
        :type endpoint: str
        :param query_params: The optional query to append to the URL
        :type query_params: dict

        :return: The full URL built from the given prefix and endpoint.
        :rtype: :py:class:``urllib.parse.ParseResult``
        """
        if endpoint is None:
            api_path = "/{base}/".format(base=prefix.strip("/"))
        elif "?" in endpoint:
            api_path = "{base}/{endpoint}".format(base=prefix, endpoint=endpoint.strip("/"))
        else:
            api_path = "{base}/{endpoint}/".format(base=prefix, endpoint=endpoint.strip("/"))
        url = self.host_url._replace(path=api_path)
        if query_params:
            url = url._replace(query=urlencode(query_params))
        return url

    def build_ui_url(self, endpoint, query_params=None):
        """Return the URL of the given endpoint in the UI API.

        :param endpoint: Usually the API object name ("users", "groups", ...)
        :type endpoint: str
        :return: The full URL built from the given endpoint.
        :rtype: :py:class:``urllib.parse.ParseResult``
        """
        return self._build_url(self.ui_path_prefix, endpoint, query_params)

    def build_plugin_url(self, endpoint, query_params=None):
        """Return the URL of the given endpoint in the UI API.

        :param endpoint: Usually the API object name ("users", "groups", ...)
        :type endpoint: str
        :return: The full URL built from the given endpoint.
        :rtype: :py:class:``urllib.parse.ParseResult``
        """
        return self._build_url(self.plugin_path_prefix, endpoint, query_params)

    def build_pulp_url(self, endpoint, query_params=None):
        """Return the URL of the given endpoint in the Pulp API.

        :param endpoint: Usually the API object name ("users", "groups", ...)
        :type endpoint: str
        :return: The full URL built from the given endpoint.
        :rtype: :py:class:``urllib.parse.ParseResult``
        """
        return self._build_url(self.pulp_path_prefix, endpoint, query_params)

    def fail_json(self, **kwargs):
        # Try to log out if we are authenticated
        if self.error_callback:
            self.error_callback(**kwargs)
        else:
            super(AHModule, self).fail_json(**kwargs)

    def exit_json(self, **kwargs):
        # Try to log out if we are authenticated
        super(AHModule, self).exit_json(**kwargs)

    def warn(self, warning):
        if self.warn_callback is not None:
            self.warn_callback(warning)
        else:
            super(AHModule, self).warn(warning)

    @staticmethod
    def get_name_field_from_endpoint(endpoint):
        return AHModule.IDENTITY_FIELDS.get(endpoint, "name")

    def get_endpoint(self, endpoint, *args, **kwargs):
        url = self.build_url(endpoint, query_params=kwargs.get("data"))
        return self.make_request("GET", url, **kwargs)

    def make_request(self, method, url, wait_for_task=True, **kwargs):
        """Perform an API call and return the data.

        :param method: GET, PUT, POST, or DELETE
        :type method: str
        :param url: URL to the API endpoint
        :type url: :py:class:``urllib.parse.ParseResult``
        :param kwargs: Additionnal parameter to pass to the API (headers, data
                       for PUT and POST requests, ...)

        :raises AHModuleError: The API request failed.

        :return: A dictionnary with two entries: ``status_code`` provides the
                 API call returned code and ``json`` provides the returned data
                 in JSON format.
        :rtype: dict
        """
        response = self.make_request_raw_reponse(method, url, **kwargs)

        try:
            response_body = response.read()
        except Exception as e:
            if response["json"]["non_field_errors"]:
                raise AHModuleError("Errors occurred with request (HTTP 400). Errors: {errors}".format(errors=response["json"]["non_field_errors"]))
            elif response["json"]["errors"]:
                raise AHModuleError("Errors occurred with request (HTTP 400). Errors: {errors}".format(errors=response["json"]["errors"]))
            elif response["text"]:
                raise AHModuleError("Errors occurred with request (HTTP 400). Errors: {errors}".format(errors=response["text"]))
            raise AHModuleError("Failed to read response body: {error}".format(error=e))

        response_json = {}
        if response_body and response_body != "":
            try:
                response_json = loads(response_body)
            except Exception as e:
                raise AHModuleError("Failed to parse the response json: {0}".format(e))

        # A background task has been triggered. Check if the task is completed
        if response.status == 202 and "task" in response_json and wait_for_task:
            url = url._replace(path=response_json["task"], query="")
            for _count in range(5):
                time.sleep(3)
                bg_task = self.make_request("GET", url)
                if "state" in bg_task["json"] and bg_task["json"]["state"].lower().startswith("complete"):
                    break
            else:
                if "state" in bg_task["json"]:
                    raise AHModuleError(
                        "Failed to get the status of the remote task: {task}: last status: {status}".format(
                            task=response_json["task"], status=bg_task["json"]["state"]
                        )
                    )
                raise AHModuleError("Failed to get the status of the remote task: {task}".format(task=response_json["task"]))

        return {"status_code": response.status, "json": response_json}

    def make_request_raw_reponse(self, method, url, **kwargs):
        # In case someone is calling us directly; make sure we were given a method, let's not just assume a GET
        if not method:
            raise Exception("The HTTP method must be defined")

        #headers = kwargs.get("headers", self.headers)

        # May need reworked
        data = None  # Important, if content type is not JSON, this should not be dict type
        # if headers.get("Content-Type", "") == "application/x-www-form-urlencoded":
        #     data = kwargs.get("data", None)
        # elif headers.get("Content-Type", "") == "application/json":
        #     data = dumps(kwargs.get("data", {}))
        # elif kwargs.get("binary", False):
        #     data = kwargs.get("data", None)

        # set default response
        response = {}
        # if url == self.authorized_url:
        #     self.fail_json(msg="{error}".format(error=self.verify_ssl))
        try:
            response = self.session.open(
                method,
                url.geturl(),
                validate_certs=self.verify_ssl,
                timeout=self.request_timeout,
                follow_redirects=True,
                data=data,
            )
        except (SSLValidationError) as ssl_err:
            self.fail_json(msg="Could not establish a secure connection to your host ({1}): {0}.".format(url.netloc, ssl_err))
        except (ConnectionError) as con_err:
            self.fail_json(msg="There was a network error of some kind trying to connect to your host ({1}): {0}.".format(url.netloc, con_err))
        except (HTTPError) as he:
            # Sanity check: Did the server send back some kind of internal error?
            if he.code >= 500:
                self.fail_json(msg="The host sent back a server error ({1}): {0}. Please check the logs and try again later".format(url.path, he))
            # Sanity check: Did we fail to authenticate properly?  If so, fail out now; this is always a failure.
            elif he.code == 401:
                self.fail_json(msg="Invalid Automation Hub authentication credentials for url:{0} headers:{1} (HTTP 401).".format(url.path, self.session.headers))
            # Sanity check: Did we get a forbidden response, which means that the user isn't allowed to do this? Report that.
            elif he.code == 403:
                self.fail_json(msg="You don't have permission to {2} , {1} to {0} (HTTP 403).".format(url.geturl(), method, self.session.headers))
            # Sanity check: Did we get a 404 response?
            # Requests with primary keys will return a 404 if there is no response, and we want to consistently trap these.
            elif he.code == 404:
                if kwargs.get("return_none_on_404", False):
                    return None
                if kwargs.get("return_errors_on_404", False):
                    page_data = he.read()
                    try:
                        return {"status_code": he.code, "json": loads(page_data)}
                    # JSONDecodeError only available on Python 3.5+
                    except ValueError:
                        return {"status_code": he.code, "text": page_data}
                self.fail_json(
                    msg="The requested object could not be found at {0}.".format(url.path),
                    response=he,
                )
            # Sanity check: Did we get a 405 response?
            # A 405 means we used a method that isn't allowed. Usually this is a bad request, but it requires special treatment because the
            # API sends it as a logic error in a few situations (e.g. trying to cancel a job that isn't running).
            elif he.code == 405:
                self.fail_json(msg="The Automation Hub server says you can't make a request with the {0} method to this endpoing {1}".format(method, url.path))
            # Sanity check: Did we get some other kind of error?  If so, write an appropriate error message.
            elif he.code >= 400:
                # We are going to return a 400 so the module can decide what to do with it
                page_data = he.read()
                try:
                    return {"status_code": he.code, "json": loads(page_data)}
                # JSONDecodeError only available on Python 3.5+
                except ValueError:
                    return {"status_code": he.code, "text": page_data}
            elif he.code == 204 and method == "DELETE":
                # A 204 is a normal response for a delete function
                pass
            else:
                self.fail_json(msg="Unexpected return code when calling {0}: {1}".format(url.geturl(), he))
        except (Exception) as e:
            self.fail_json(msg="There was an unknown error when trying to connect to {2}: {0} {1}".format(type(e).__name__, e, url.geturl()))

        return response


    def get_one(self, endpoint, name_or_id=None, allow_none=True, **kwargs):
        new_kwargs = kwargs.copy()
        if name_or_id:
            name_field = self.get_name_field_from_endpoint(endpoint)
            new_data = kwargs.get("data", {}).copy()
            if name_field in new_data:
                self.fail_json(msg="You can't specify the field {0} in your search data if using the name_or_id field".format(name_field))

            try:
                new_data["or__id"] = int(name_or_id)
                new_data["or__{0}".format(name_field)] = name_or_id
            except ValueError:
                # If we get a value error, then we didn't have an integer so we can just pass and fall down to the fail
                new_data[name_field] = name_or_id
            new_kwargs["data"] = new_data

        response = self.get_endpoint(endpoint, **new_kwargs)
        if response["status_code"] != 200:
            fail_msg = "Got a {0} response when trying to get one from {1}".format(response["status_code"], endpoint)
            if "detail" in response.get("json", {}):
                fail_msg += ", detail: {0}".format(response["json"]["detail"])
            self.fail_json(msg=fail_msg)

        if "count" not in response["json"]["meta"] or "data" not in response["json"]:
            self.fail_json(msg="The endpoint did not provide count and results.")

        if response["json"]["meta"]["count"] == 0:
            if allow_none:
                return None
            else:
                self.fail_wanted_one(response, endpoint, new_kwargs.get("data"))
        elif response["json"]["meta"]["count"] > 1:
            if name_or_id:
                # Since we did a name or ID search and got > 1 return something if the id matches
                for asset in response["json"]["data"]:
                    if str(asset["id"]) == name_or_id:
                        return self.existing_item_add_url(asset, endpoint)

            # We got > 1 and either didn't find something by ID (which means multiple names)
            # Or we weren't running with a or search and just got back too many to begin with.
            self.fail_wanted_one(response, endpoint, new_kwargs.get("data"))

        return self.existing_item_add_url(response["json"]["data"][0], endpoint)

    def get_only(self, endpoint, name_or_id=None, allow_none=True, key="url", **kwargs):
        new_kwargs = kwargs.copy()
        if name_or_id:
            name_field = self.get_name_field_from_endpoint(endpoint)
            new_data = kwargs.get("data", {}).copy()
            if name_field in new_data:
                self.fail_json(msg="You can't specify the field {0} in your search data if using the name_or_id field".format(name_field))

            try:
                new_data["or__id"] = int(name_or_id)
                new_data["or__{0}".format(name_field)] = name_or_id
            except ValueError:
                # If we get a value error, then we didn't have an integer so we can just pass and fall down to the fail
                new_data[name_field] = name_or_id
            new_kwargs["data"] = new_data

        response = self.get_endpoint(endpoint, **new_kwargs)
        if response["status_code"] != 200:
            fail_msg = "Got a {0} response when trying to get from {1}".format(response["status_code"], endpoint)
            if "detail" in response.get("json", {}):
                fail_msg += ", detail: {0}".format(response["json"]["detail"])
            self.fail_json(msg=fail_msg)

        return self.existing_item_add_url(response["json"], endpoint, key=key)

    def keycloak_auth(self, **kwargs):
        try:
            response = self.session.open(
                "POST",
                self.auth_url,
                validate_certs=self.verify_ssl,
                timeout=self.request_timeout,
                follow_redirects=True,
                data = 'grant_type=refresh_token&client_id=%s&refresh_token=%s' % (self.client_id,
                                                                           self.access_token)
            )
            data = loads(to_text(response.read(), errors='surrogate_or_strict'))
            external_token = data.get('access_token')
            self.authenticated = True
            return external_token

        except (Exception) as e:
            # Sanity check: Did the server send back some kind of internal error?
            self.fail_json(msg="Failed to use auth server: {0}".format(e))

    def basic_authenticate(self, **kwargs):
        # Attempt to get a token from /v3/auth/token/ by giving it our username/password combo
        # If we have a username and password, we need to get a session cookie
        api_token_url = self.build_url("auth/token").geturl()
        try:
            try:
                self.session.open(
                    "POST",
                    api_token_url,
                    validate_certs=self.verify_ssl,
                    timeout=self.request_timeout,
                    follow_redirects=True,
                    force_basic_auth=True,
                    url_username=self.username,
                    url_password=self.password,
                    headers={"Content-Type": "application/json"},
                )
            except HTTPError:
                test_url = self.build_url("namespaces").geturl()
                self.basic_auth = True
                basic_str = base64.b64encode("{0}:{1}".format(self.username, self.password).encode("ascii"))
                self.session.open(
                    "GET",
                    test_url,
                    validate_certs=self.verify_ssl,
                    timeout=self.request_timeout,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Basic {0}".format(basic_str.decode("ascii")),
                    },
                )
        except HTTPError as he:
            try:
                resp = he.read()
            except Exception as e:
                resp = "unknown {0}".format(e)
            self.fail_json(msg="Failed to use basic auth: {0}".format(he), response=resp)
        except (Exception) as e:
            # Sanity check: Did the server send back some kind of internal error?
            self.fail_json(msg="Failed to use basic auth: {0}".format(e))

        # If we have neither of these, then we can try un-authenticated access
        self.authenticated = True

    def existing_item_add_url(self, existing_item, endpoint, key="url"):
        # Add url and type to response as its missing in current iteration of Automation Hub.
        existing_item[key] = "{0}{1}/".format(self.build_url(endpoint).geturl()[len(self.host):], existing_item["name"])
        existing_item["type"] = endpoint
        return existing_item

    def delete_if_needed(self, existing_item, on_delete=None, auto_exit=True):
        # This will exit from the module on its own.
        # If the method successfully deletes an item and on_delete param is defined,
        #   the on_delete parameter will be called as a method pasing in this object and the json from the response
        # This will return one of two things:
        #   1. None if the existing_item is not defined (so no delete needs to happen)
        #   2. The response from Automation Hub from calling the delete on the endpont. It's up to you to process the response and exit from the module
        # Note: common error codes from the Automation Hub API can cause the module to fail
        if existing_item:
            if existing_item["type"] == "token":
                response = self.delete_endpoint(existing_item["endpoint"])
            else:
                # If we have an item, we can try to delete it
                try:
                    item_url = existing_item["url"]
                    item_type = existing_item["type"]
                    item_id = existing_item["id"]
                    item_name = self.get_item_name(existing_item, allow_unknown=True)
                except KeyError as ke:
                    self.fail_json(msg="Unable to process delete of item due to missing data {0}".format(ke))
                response = self.delete_endpoint(item_url)
        else:
            if auto_exit:
                self.exit_json(**self.json_output)
            else:
                return self.json_output

        if response["status_code"] in [202, 204]:
            if on_delete:
                on_delete(self, response["json"])
            self.json_output["changed"] = True
            if existing_item["type"] == "token":
                self.json_output["msg"] = "Token Revoked"
                self.exit_json(**self.json_output)
            else:
                self.json_output["id"] = item_id
                self.exit_json(**self.json_output)
            if auto_exit:
                self.exit_json(**self.json_output)
            else:
                return self.json_output
        else:
            if "json" in response and "__all__" in response["json"]:
                self.fail_json(msg="Unable to delete {0} {1}: {2}".format(item_type, item_name, response["json"]["__all__"][0]))
            elif "json" in response:
                # This is from a project delete (if there is an active job against it)
                if "error" in response["json"]:
                    self.fail_json(msg="Unable to delete {0} {1}: {2}".format(item_type, item_name, response["json"]["error"]))
                else:
                    self.fail_json(msg="Unable to delete {0} {1}: {2}".format(item_type, item_name, response["json"]))
            else:
                self.fail_json(msg="Unable to delete {0} {1}: {2}".format(item_type, item_name, response["status_code"]))

    def get_item_name(self, item, allow_unknown=False):
        if item:
            if "name" in item:
                return item["name"]

        if allow_unknown:
            return "unknown"

        if item:
            self.exit_json(msg="Cannot determine identity field for {0} object.".format(item.get("type", "unknown")))
        else:
            self.exit_json(msg="Cannot determine identity field for Undefined object.")

    def delete_endpoint(self, endpoint, *args, **kwargs):
        # Handle check mode
        if self.check_mode:
            self.json_output["changed"] = True
            self.exit_json(**self.json_output)

        return self.make_request("DELETE", endpoint, **kwargs)

    def create_or_update_if_needed(
        self,
        existing_item,
        new_item,
        endpoint=None,
        item_type="unknown",
        on_create=None,
        on_update=None,
        auto_exit=True,
        associations=None,
        require_id=True,
        fixed_url=None,
    ):
        if existing_item:
            return self.update_if_needed(
                existing_item,
                new_item,
                on_update=on_update,
                auto_exit=auto_exit,
                associations=associations,
                require_id=require_id,
                fixed_url=fixed_url,
            )
        else:
            return self.create_if_needed(
                existing_item,
                new_item,
                endpoint,
                on_create=on_create,
                item_type=item_type,
                auto_exit=auto_exit,
                associations=associations,
            )

    def create_if_needed(
        self,
        existing_item,
        new_item,
        endpoint,
        on_create=None,
        auto_exit=True,
        item_type="unknown",
        associations=None,
    ):

        # This will exit from the module on its own
        # If the method successfully creates an item and on_create param is defined,
        #    the on_create parameter will be called as a method pasing in this object and the json from the response
        # This will return one of two things:
        #    1. None if the existing_item is already defined (so no create needs to happen)
        #    2. The response from Automation Hub from calling the patch on the endpont. It's up to you to process the response and exit from the module
        # Note: common error codes from the Automation Hub API can cause the module to fail

        if not endpoint:
            self.fail_json(msg="Unable to create new {0} due to missing endpoint".format(item_type))

        item_url = None
        if existing_item:
            try:
                item_url = existing_item["url"]
            except KeyError as ke:
                self.fail_json(msg="Unable to process create of item due to missing data {0}".format(ke))
        else:
            # If we don't have an exisitng_item, we can try to create it

            # We have to rely on item_type being passed in since we don't have an existing item that declares its type
            # We will pull the item_name out from the new_item, if it exists
            item_name = self.get_item_name(new_item, allow_unknown=True)

            response = self.post_endpoint(endpoint, **{"data": new_item})

            if response["status_code"] in [200, 201]:
                self.json_output["name"] = "unknown"
                for key in ("name", "username", "identifier", "hostname"):
                    if key in response["json"]:
                        self.json_output["name"] = response["json"][key]
                if item_type != "token":
                    self.json_output["id"] = response["json"]["id"]
                    item_url = "{0}{1}/".format(
                        self.build_url(endpoint).geturl()[len(self.host):],
                        new_item["name"],
                    )
                self.json_output["changed"] = True
            else:
                if "json" in response and "__all__" in response["json"]:
                    self.fail_json(msg="Unable to create {0} {1}: {2}".format(item_type, item_name, response["json"]["__all__"][0]))
                elif "json" in response:
                    self.fail_json(msg="Unable to create {0} {1}: {2}".format(item_type, item_name, response["json"]))
                else:
                    self.fail_json(msg="Unable to create {0} {1}: {2}".format(item_type, item_name, response["status_code"]))

        # Process any associations with this item
        if associations is not None:
            for association_type in associations:
                sub_endpoint = "{0}{1}/".format(item_url, association_type)
                self.modify_associations(sub_endpoint, associations[association_type])

        # If we have an on_create method and we actually changed something we can call on_create
        if on_create is not None and self.json_output["changed"]:
            on_create(self, response["json"])
        elif auto_exit:
            self.exit_json(**self.json_output)
        else:
            last_data = response["json"]
            return last_data

    def approve(self, endpoint, timeout=None, interval=10.0, auto_exit=True):

        approvalEndpoint = "move/staging/published"

        if not endpoint:
            self.fail_json(msg="Unable to approve due to missing endpoint")

        response = self.post_endpoint("{0}/{1}".format(endpoint, approvalEndpoint), None, **{"return_none_on_404": True})

        i = 0
        while timeout is None or i < timeout:
            if not response:
                time.sleep(interval)
                response = self.post_endpoint("{0}/{1}".format(endpoint, approvalEndpoint), None, **{"return_none_on_404": True})
                i += interval
            else:
                break

        if response and response["status_code"] in [202]:
            self.json_output["changed"] = True
        else:
            # Do a check to see if the version exists
            if not response:
                self.fail_json(msg="Unable to approve at {0}: Awaiting approval not found".format(endpoint))
            elif "json" in response and "__all__" in response["json"]:
                self.fail_json(msg="Unable to approve at {0}: {1}".format(endpoint, response["json"]["__all__"][0]))
            elif "json" in response:
                self.fail_json(msg="Unable to create {0}: {1}".format(endpoint, response["json"]))
            else:
                self.fail_json(msg="Unable to create {0}: {1}".format(endpoint, response["status_code"]))

        if auto_exit:
            self.exit_json(**self.json_output)
        else:
            last_data = response["json"]
            return last_data

    def prepare_multipart(self, filename):
        mime = "application/x-gzip"
        m = email.mime.multipart.MIMEMultipart("form-data")

        main_type, sep, sub_type = mime.partition("/")

        with open(to_bytes(filename, errors="surrogate_or_strict"), "rb") as f:
            part = email.mime.application.MIMEApplication(f.read())
            del part["Content-Type"]
            part.add_header("Content-Type", "%s/%s" % (main_type, sub_type))

        part.add_header("Content-Disposition", "form-data")
        del part["MIME-Version"]
        part.set_param("name", "file", header="Content-Disposition")
        if filename:
            part.set_param(
                "filename",
                to_native(os.path.basename(filename)),
                header="Content-Disposition",
            )

        m.attach(part)

        if PY3:
            # Ensure headers are not split over multiple lines
            # The HTTP policy also uses CRLF by default
            b_data = m.as_bytes(policy=email.policy.HTTP)
        else:
            # Py2
            # We cannot just call ``as_string`` since it provides no way
            # to specify ``maxheaderlen``
            # cStringIO seems to be required here
            fp = cStringIO()  # noqa: F821 # pylint: disable=undefined-variable
            # Ensure headers are not split over multiple lines
            g = email.generator.Generator(fp, maxheaderlen=0)
            g.flatten(m)
            # ``fix_eols`` switches from ``\n`` to ``\r\n``
            b_data = email.utils.fix_eols(fp.getvalue())
        del m

        headers, sep, b_content = b_data.partition(b"\r\n\r\n")
        del b_data

        if PY3:
            parser = email.parser.BytesHeaderParser().parsebytes
        else:
            parser = email.parser.HeaderParser().parsestr

        return (
            parser(headers)["content-type"],
            b_content,
        )  # Message converts to native strings

    def getFileContent(self, path):
        try:
            with open(to_bytes(path, errors="surrogate_or_strict"), "rb") as f:
                b_file_data = f.read()
            return to_text(b_file_data)
        except FileNotFoundError:
            self.fail_json(msg="No such file found on the local filesystem: '{0}'".format(path))

    def wait_for_complete(self, task_url):
        endpoint = task_url
        state = "running"
        while state == "running":
            response = self.get_endpoint(endpoint)
            state = response["json"]["state"]
            time.sleep(1)
        self.json_output["state"] = state
        if state == "failed":
            self.fail_json(msg="Upload of collection failed: {0}".format(response["json"]["error"]["description"]))
        else:
            time.sleep(1)
            return

    def upload(self, path, endpoint, wait=True, item_type="unknown"):
        if "://" in path:
            tmppath = fetch_file(self, path)
            path = ".".join(tmppath.split(".")[:-2]) + ".tar.gz"
            os.rename(tmppath, path)
            self.add_cleanup_file(path)
        ct, body = self.prepare_multipart(path)
        response = self.make_request(
            "POST",
            endpoint,
            **{
                "data": body,
                "headers": {"Content-Type": str(ct)},
                "binary": True,
                "return_errors_on_404": True,
            }
        )
        if response["status_code"] in [202]:
            self.json_output["path"] = path
            self.json_output["changed"] = True
            if wait:
                self.wait_for_complete(response["json"]["task"])
            return
        else:
            if "json" in response and "__all__" in response["json"]:
                self.fail_json(msg="Unable to create {0} from {1}: {2}".format(item_type, path, response["json"]["__all__"][0]))
            elif "json" in response and "errors" in response["json"] and "detail" in response["json"]["errors"][0]:
                self.fail_json(msg="Unable to create {0} from {1}: {2}".format(item_type, path, response["json"]["errors"][0]["detail"]))
            elif "json" in response:
                self.fail_json(msg="Unable to create {0} from {1}: {2}".format(item_type, path, response["json"]))
            else:
                self.fail_json(msg="Unable to create {0} from {1}: {2}".format(item_type, path, response["status_code"]))

    def update_if_needed(
        self,
        existing_item,
        new_item,
        on_update=None,
        auto_exit=True,
        associations=None,
        require_id=True,
        fixed_url=None,
    ):
        # This will exit from the module on its own
        # If the method successfully updates an item and on_update param is defined,
        #   the on_update parameter will be called as a method pasing in this object and the json from the response
        # This will return one of two things:
        #    1. None if the existing_item does not need to be updated
        #    2. The response from Automation Hub from patching to the endpoint. It's up to you to process the response and exit from the module.
        # Note: common error codes from the Automation Hub API can cause the module to fail
        response = None
        if existing_item:
            # If we have an item, we can see if it needs an update
            try:
                item_url = fixed_url or existing_item["url"]
                item_type = existing_item["type"]
                item_name = existing_item["name"]
                item_id = require_id and existing_item["id"]
            except KeyError as ke:
                self.fail_json(msg="Unable to process update of item due to missing data {0}".format(ke))

            # Check to see if anything within the item requires the item to be updated
            needs_patch = self.objects_could_be_different(existing_item, new_item)

            # If we decided the item needs to be updated, update it
            self.json_output["id"] = item_id
            self.json_output["name"] = item_name
            self.json_output["type"] = item_type
            if needs_patch:
                response = self.put_endpoint(item_url, **{"data": new_item})
                if response["status_code"] == 200:
                    # compare apples-to-apples, old API data to new API data
                    # but do so considering the fields given in parameters
                    self.json_output["changed"] = self.objects_could_be_different(
                        existing_item,
                        response["json"],
                        field_set=new_item.keys(),
                        warning=True,
                    )
                elif "json" in response and "__all__" in response["json"]:
                    self.fail_json(msg=response["json"]["__all__"])
                else:
                    self.fail_json(
                        **{
                            "msg": "Unable to update {0} {1}, see response".format(item_type, item_name),
                            "response": response,
                            "input": new_item,
                        }
                    )

        else:
            raise RuntimeError("update_if_needed called incorrectly without existing_item")

        # Process any associations with this item
        if associations is not None:
            for association_type, id_list in associations.items():
                endpoint = "{0}{1}/".format(item_url, association_type)
                self.modify_associations(endpoint, id_list)

        # If we change something and have an on_change call it
        if on_update is not None and self.json_output["changed"]:
            if response is None:
                last_data = existing_item
            else:
                last_data = response["json"]
            on_update(self, last_data)
        elif auto_exit:
            self.exit_json(**self.json_output)
        else:
            if response is None:
                last_data = existing_item
            else:
                last_data = response["json"]
            return last_data

    def modify_associations(self, association_endpoint, new_association_list):
        # if we got None instead of [] we are not modifying the association_list
        if new_association_list is None:
            return

        # First get the existing associations
        response = self.get_all_endpoint(association_endpoint)
        existing_associated_ids = [association["id"] for association in response["json"]["results"]]

        # Disassociate anything that is in existing_associated_ids but not in new_association_list
        ids_to_remove = list(set(existing_associated_ids) - set(new_association_list))
        for an_id in ids_to_remove:
            response = self.post_endpoint(association_endpoint, **{"data": {"id": int(an_id), "disassociate": True}})
            if response["status_code"] == 204:
                self.json_output["changed"] = True
            else:
                self.fail_json(msg="Failed to disassociate item {0}".format(response["json"].get("detail", response["json"])))

        # Associate anything that is in new_association_list but not in `association`
        for an_id in list(set(new_association_list) - set(existing_associated_ids)):
            response = self.post_endpoint(association_endpoint, **{"data": {"id": int(an_id)}})
            if response["status_code"] == 204:
                self.json_output["changed"] = True
            else:
                self.fail_json(msg="Failed to associate item {0}".format(response["json"].get("detail", response["json"])))

    def post_endpoint(self, endpoint, *args, **kwargs):
        # Handle check mode
        if self.check_mode:
            self.json_output["changed"] = True
            self.exit_json(**self.json_output)
        url = self.build_url(endpoint, query_params=kwargs.get("data"))
        return self.make_request("POST", url, **kwargs)

    def patch_endpoint(self, endpoint, *args, **kwargs):
        # Handle check mode
        if self.check_mode:
            self.json_output["changed"] = True
            self.exit_json(**self.json_output)
        url = self.build_url(endpoint, query_params=kwargs.get("data"))
        return self.make_request("PATCH", url, **kwargs)

    def put_endpoint(self, endpoint, *args, **kwargs):
        # Handle check mode
        if self.check_mode:
            self.json_output["changed"] = True
            self.exit_json(**self.json_output)
        url = self.build_url(endpoint, query_params=kwargs.get("data"))
        return self.make_request("PUT", url, **kwargs)

    def get_all_endpoint(self, endpoint, *args, **kwargs):
        response = self.get_endpoint(endpoint, *args, **kwargs)
        if "next" not in response["json"]:
            raise RuntimeError("Expected list from API at {0}, got: {1}".format(endpoint, response))
        next_page = response["json"]["next"]

        if response["json"]["count"] > 10000:
            self.fail_json(msg="The number of items being queried for is higher than 10,000.")

        while next_page is not None:
            next_response = self.get_endpoint(next_page)
            response["json"]["results"] = response["json"]["results"] + next_response["json"]["results"]
            next_page = next_response["json"]["next"]
            response["json"]["next"] = next_page
        return response

    def fail_wanted_one(self, response, endpoint, query_params):
        sample = response.copy()
        if len(sample["json"]["data"]) > 1:
            sample["json"]["data"] = sample["json"]["data"][:2] + ["...more results snipped..."]
        url = self.build_url(endpoint, query_params)
        display_endpoint = url.geturl()[len(self.host):]  # truncate to not include the base URL
        self.fail_json(
            msg="Request to {0} returned {1} items, expected 1".format(display_endpoint, response["json"]["meta"]["count"]),
            query=query_params,
            response=sample,
            total_results=response["json"]["meta"]["count"],
        )

    def get_exactly_one(self, endpoint, name_or_id=None, **kwargs):
        return self.get_one(endpoint, name_or_id=name_or_id, allow_none=False, **kwargs)

    def resolve_name_to_id(self, endpoint, name_or_id):
        return self.get_exactly_one(endpoint, name_or_id)["id"]

    def objects_could_be_different(self, old, new, field_set=None, warning=False):
        if field_set is None:
            field_set = set(fd for fd in new.keys() if fd not in ("modified", "related", "summary_fields"))
        for field in field_set:
            new_field = new.get(field, None)
            old_field = old.get(field, None)
            if old_field != new_field:
                if self.update_secrets or (not self.fields_could_be_same(old_field, new_field)):
                    return True  # Something doesn't match, or something might not match
            elif self.has_encrypted_values(new_field) or field not in new:
                if self.update_secrets or (not self.fields_could_be_same(old_field, new_field)):
                    # case of 'field not in new' - user password write-only field that API will not display
                    self._encrypted_changed_warning(field, old, warning=warning)
                    return True
        return False

    def execute_build(self, path, force, output_path):
        path = self._resolve_path(path)
        output_path = self._resolve_path(output_path)
        b_output_path = to_bytes(output_path, errors="surrogate_or_strict")

        if not os.path.exists(b_output_path):
            os.makedirs(b_output_path)
        elif os.path.isfile(b_output_path):
            self.fail_json(msg="the output collection directory {0} is a file - aborting".format(to_native(output_path)))

        output_build = self.run_command(
            [
                "ansible-galaxy",
                "collection",
                "build",
                path,
                "--output-path",
                output_path,
                (None, "--force")[force],
            ]
        )
        if output_build[0] == 0:
            self.json_output["path"] = "/" + "/".join(output_build[1].split("/")[1:])[:-1]
            self.json_output["changed"] = True
            self.exit_json(**self.json_output)
        else:
            self.fail_json(msg=output_build[2])

    def wait_sync_output(self, response):
        for k in ("task_id", "state", "started_at", "finished_at"):
            self.json_output[k] = response["last_sync_task"].get(k)

    @staticmethod
    def _resolve_path(path):
        return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))

    @staticmethod
    def has_encrypted_values(obj):
        """Returns True if JSON-like python content in obj has $encrypted$
        anywhere in the data as a value
        """
        if isinstance(obj, dict):
            for val in obj.values():
                if AHModule.has_encrypted_values(val):
                    return True
        elif isinstance(obj, list):
            for val in obj:
                if AHModule.has_encrypted_values(val):
                    return True
        elif obj == AHModule.ENCRYPTED_STRING:
            return True
        return False

    def _encrypted_changed_warning(self, field, old, warning=False):
        if not warning:
            return
        self.warn(
            "The field {0} of {1} {2} has encrypted data and may inaccurately report task is changed.".format(
                field, old.get("type", "unknown"), old.get("id", "unknown")
            )
        )
