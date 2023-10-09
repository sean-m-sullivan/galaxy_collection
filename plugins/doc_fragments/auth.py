# -*- coding: utf-8 -*-

# Copyright: (c) 2020, Sean Sullivan <@sean-m-sullivan>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function

__metaclass__ = type


class ModuleDocFragment(object):

    # Ansible Galaxy documentation fragment
    DOCUMENTATION = r"""
options:
  ah_host:
    description:
    - URL to Ansible Galaxy or Automation Hub instance.
    - If value not set, will try environment variable C(AH_HOST)
    - If value not specified by any means, the value of C(127.0.0.1) will be used
    type: str
    aliases: [ ah_hostname ]
  ah_username:
    description:
    - Username for your Ansible Galaxy or Automation Hub instance.
    - If value not set, will try environment variable C(AH_USERNAME)
    type: str
  ah_password:
    description:
    - Password for your Ansible Galaxy or Automation Hub instance.
    - If value not set, will try environment variable C(AH_PASSWORD)
    type: str
  ah_token:
    description:
    - The Ansible Galaxy or Automation Hub API token to use.
    - This value can be in one of two formats.
    - A string which is the token itself. (i.e. bqV5txm97wqJqtkxlMkhQz0pKhRMMX)
    - A dictionary structure as returned by the ah_token module.
    - If value not set, will try environment variable C(AH_API_TOKEN)
    type: raw
  ah_sso_token:
    description:
    - The SSO API token to use.
    - This value can be in one of two formats.
    - A string which is the token itself. (i.e. bqV5txm97wqJqtkxlMkhQz0pKhRMMX)
    - If value not set, will try environment variable C(AH_SSO_TOKEN)
    type: raw
  ah_cookie:
    description:
    - The Ansible Galaxy or Automation Hub API Cookie to use.
    - This is an alternative to a token, How to get it TBD
    - A string which is the cookie itself. (i.e. 9u52tnJfWo5Gvnk31HULwKK3BE9NuGw7GImrFj3vsTSuvvB1UTl4o219ixf4BcqX)
    - If value not set, will try environment variable C(AH_API_COOKIE)
    type: str
  validate_certs:
    description:
    - Whether to allow insecure connections to Galaxy or Automation Hub Server.
    - If C(no), SSL certificates will not be validated.
    - This should only be used on personally controlled sites using self-signed certificates.
    - If value not set, will try environment variable C(AH_VERIFY_SSL)
    type: bool
    aliases: [ ah_verify_ssl ]
  request_timeout:
    description:
    - Specify the timeout Ansible should use in requests to the Galaxy or Automation Hub host.
    - Defaults to 10s, but this is handled by the shared module_utils code
    type: float
  ah_path_prefix:
    description:
    - API path used to access the api.
    - For galaxy_ng this is either 'automation-hub' or the custom prefix used on install.
    - For Automation Hub this is 'galaxy'
    - If value not set, will try environment variable C(GALAXY_API_PATH_PREFIX)
    type: str
    default: 'galaxy'
  ah_auth_url:
    description:
    - SSO URL.
    - Use this URL to configure the authentication URLs that clients need to download content from Automation Hub.
    - For console.redhat.com it should look like this 'https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token'.
    - For SSO it should look like this 'https://sso.nas:8443/auth/realms/ansible-automation-platform/protocol/openid-connect/token'.
    - If value not set, will try environment variable C(AH_AUTH_URL)
    type: str
  client_id:
    description:
    - API path used to access the api.
    - For Red Hat SSO console.redhat.com this is 'cloud-services'.
    - For Automation Hub Installed SSO this is 'automation-hub'.
    - For Galaxy-ng setup with Keycloak this is 'galaxy-ng'.
    - If not setup with traditional installers, check the client-id in the SSO server.
    - If value not set, will try environment variable C(AH_CLIENT_ID)
    type: str
    default: 'galaxy-ng'
"""
