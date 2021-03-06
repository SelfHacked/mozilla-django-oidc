import logging
import time
import requests

try:
    from urllib.parse import urlencode
except ImportError:
    # Python < 3
    from urllib import urlencode

from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.urls import reverse
from django.contrib.auth import BACKEND_SESSION_KEY, logout as django_logout

from django.http import HttpResponseRedirect, JsonResponse
from django.utils.crypto import get_random_string
from django.utils.deprecation import MiddlewareMixin
from django.utils.functional import cached_property
from django.utils.module_loading import import_string

from mozilla_django_oidc.auth import (
    OIDCAuthenticationBackend,
    store_tokens,
    store_expiration_times,
)
from mozilla_django_oidc.utils import (
    absolutify, add_state_and_nonce_to_session, import_from_settings,
)


LOGGER = logging.getLogger(__name__)


class SessionRefresh(MiddlewareMixin):
    """Refreshes the session with the OIDC RP after expiry seconds

    For users authenticated with the OIDC RP, verify tokens are still valid and
    if not, force the user to re-authenticate silently.

    """

    @staticmethod
    def get_settings(attr, *args):
        return import_from_settings(attr, *args)

    @cached_property
    def exempt_urls(self):
        """Generate and return a set of url paths to exempt from SessionRefresh

        This takes the value of ``settings.OIDC_EXEMPT_URLS`` and appends three
        urls that mozilla-django-oidc uses. These values can be view names or
        absolute url paths.

        :returns: list of url paths (for example "/oidc/callback/")

        """
        exempt_urls = list(self.get_settings('OIDC_EXEMPT_URLS', []))
        exempt_urls.extend([
            'oidc_authentication_init',
            'oidc_authentication_callback',
            'oidc_logout',
        ])

        return set([
            url if url.startswith('/') else reverse(url)
            for url in exempt_urls
        ])

    def is_refreshable_url(self, request, get_only):
        """Takes a request and returns whether it triggers a refresh examination

        :arg HttpRequest request:
        :arg bool get_only:

        :returns: boolean

        """
        # Do not attempt to refresh the session if the OIDC backend is not used
        backend_session = request.session.get(BACKEND_SESSION_KEY)
        is_oidc_enabled = True
        if backend_session:
            auth_backend = import_string(backend_session)
            is_oidc_enabled = issubclass(auth_backend, OIDCAuthenticationBackend)

        return (
            (not get_only or request.method == 'GET') and
            request.user.is_authenticated and
            is_oidc_enabled and
            request.path not in self.exempt_urls
        )

    def is_expired(self, request):
        expiration = request.session.get('oidc_id_token_expiration', 0)
        now = time.time()
        if expiration > now:
            # The id_token is still valid, so we don't have to do anything.
            LOGGER.debug('id token is still valid (%s > %s)', expiration, now)
            return False

        return True

    def process_request(self, request):
        if not self.is_refreshable_url(request, get_only=True):
            LOGGER.debug('request is not refreshable')
            return

        if not self.is_expired(request):
            return

        LOGGER.debug('id token has expired')
        # The id_token has expired, so we have to re-authenticate silently.
        auth_url = self.get_settings('OIDC_OP_AUTHORIZATION_ENDPOINT')
        client_id = self.get_settings('OIDC_RP_CLIENT_ID')
        state = get_random_string(self.get_settings('OIDC_STATE_SIZE', 32))

        # Build the parameters as if we were doing a real auth handoff, except
        # we also include prompt=none.
        params = {
            'response_type': 'code',
            'client_id': client_id,
            'redirect_uri': absolutify(
                request,
                reverse(self.get_settings('OIDC_AUTHENTICATION_CALLBACK_URL',
                                          'oidc_authentication_callback'))
            ),
            'state': state,
            'scope': self.get_settings('OIDC_RP_SCOPES', 'openid email'),
            'prompt': 'none',
        }

        if self.get_settings('OIDC_USE_NONCE', True):
            nonce = get_random_string(self.get_settings('OIDC_NONCE_SIZE', 32))
            params.update({
                'nonce': nonce
            })

        add_state_and_nonce_to_session(request, state, params)

        request.session['oidc_login_next'] = request.get_full_path()

        query = urlencode(params)
        redirect_url = '{url}?{query}'.format(url=auth_url, query=query)
        if request.is_ajax():
            # Almost all XHR request handling in client-side code struggles
            # with redirects since redirecting to a page where the user
            # is supposed to do something is extremely unlikely to work
            # in an XHR request. Make a special response for these kinds
            # of requests.
            # The use of 403 Forbidden is to match the fact that this
            # middleware doesn't really want the user in if they don't
            # refresh their session.
            response = JsonResponse({'refresh_url': redirect_url}, status=403)
            response['refresh_url'] = redirect_url
            return response
        return HttpResponseRedirect(redirect_url)


class RefreshOIDCToken(SessionRefresh):
    """
    A middleware that will refresh the access token following proper OIDC protocol:
    https://auth0.com/docs/tokens/refresh-token/current
    """
    def process_request(self, request):
        if not self.is_refreshable_url(request, get_only=False):
            LOGGER.debug('request is not refreshable')
            return

        if not self.is_expired(request):
            return

        token_url = import_from_settings('OIDC_OP_TOKEN_ENDPOINT')
        client_id = import_from_settings('OIDC_RP_CLIENT_ID')
        client_secret = import_from_settings('OIDC_RP_CLIENT_SECRET')
        refresh_token = request.session.get('oidc_refresh_token')

        if self._is_refresh_token_expired(request):
            return self._handle_refresh_token_expire(request)

        if not refresh_token:
            LOGGER.debug('no refresh token stored')
            raise ImproperlyConfigured('Refresh token missing.')

        token_payload = {
            'grant_type': 'refresh_token',
            'client_id': client_id,
            'client_secret': client_secret,
            'refresh_token': refresh_token,
        }

        response = requests.post(
            token_url,
            data=token_payload,
        )
        if response.status_code != 200:
            LOGGER.info('Error renewing refresh token.')
            return self._handle_refresh_token_expire(request)

        token_info = response.json()
        id_token = token_info.get('id_token')
        access_token = token_info.get('access_token')
        refresh_token = token_info.get('refresh_token')

        store_expiration_times(request.session)
        store_tokens(request.session, access_token, id_token, refresh_token)

    def _handle_refresh_token_expire(self, request):
        renew_refresh_token = import_from_settings(
            'OIDC_RENEW_REFRESH_TOKEN', False,
        )
        # Since SessionRefresh ignore POST requests, refresh tokens
        # expired during POST requests are not passed to super class.
        if renew_refresh_token and request.method.upper() == 'GET':
            return super(RefreshOIDCToken, self).process_request(request)
        else:
            # Force logout the user to manually login again with a
            # valid session.
            django_logout(request)
            raise PermissionDenied('Refresh token expired on POST.')

    @staticmethod
    def _is_refresh_token_expired(request):
        refresh_toke_expire_time = import_from_settings(
            'OIDC_RENEW_REFRESH_TOKEN_EXPIRY_SECONDS', 0,
        )
        if not refresh_toke_expire_time:
            return False

        refresh_token_expire = (
            request.session.get('oidc_id_token_expiration', 0)
            - import_from_settings('OIDC_RENEW_ID_TOKEN_EXPIRY_SECONDS')
            + refresh_toke_expire_time
        )
        now = time.time()
        if refresh_token_expire > now:
            # The refresh_token is still valid, we don't have to do anything.
            LOGGER.debug(
                'refresh token is still valid (%s > %s)',
                refresh_token_expire, now,
            )
            return False

        return True
