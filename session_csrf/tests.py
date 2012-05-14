from collections import namedtuple
from contextlib import contextmanager

from django.conf import settings
import django.test
from django import http
from django.conf.urls.defaults import patterns
from django.contrib.auth import logout
from django.contrib.auth.middleware import AuthenticationMiddleware
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.core import signals
from django.core.cache import cache
from django.core.handlers.wsgi import WSGIRequest
from django.db import close_connection
from django.http import HttpResponse
from django.template import RequestContext, Template, context

import mock

import session_csrf
from session_csrf import CsrfMiddleware, anonymous_csrf, anonymous_csrf_exempt


def view_with_token(request):
    """Returns a response containing the CSRF token."""
    t = Template("{{csrf_token}}")
    context = RequestContext(request)
    return HttpResponse(t.render(context))


def view_without_token(request):
    """
    Returns a response rendered with a RequestContext that does not use
    csrf_token.
    """
    t = Template("")
    context = RequestContext(request)
    return HttpResponse(t.render(context))


urlpatterns = patterns('',
    ('^$', lambda r: http.HttpResponse()),
    ('^anon$', anonymous_csrf(lambda r: http.HttpResponse())),
    ('^no-anon-csrf$', anonymous_csrf_exempt(lambda r: http.HttpResponse())),
    ('^logout$', anonymous_csrf(lambda r: logout(r) or http.HttpResponse())),
    ('^token$', view_with_token),
    ('^no-token$', view_without_token),
)


class TestCsrfToken(django.test.TestCase):
    urls = 'session_csrf.tests'

    def setUp(self):
        self.client.handler = ClientHandler()
        User.objects.create_user('jbalogh', 'j@moz.com', 'password')
        self.save_ANON_ALWAYS = session_csrf.ANON_ALWAYS
        session_csrf.ANON_ALWAYS = False

    def tearDown(self):
        session_csrf.ANON_ALWAYS = self.save_ANON_ALWAYS

    def login(self):
        assert self.client.login(username='jbalogh', password='password')

    def test_csrftoken_unauthenticated(self):
        # request.META['CSRF_COOKIE'] is '' for anonymous users.
        response = self.client.get('/', follow=True)
        self.assertEqual(response._request.META['CSRF_COOKIE'], '')

    def test_csrftoken_authenticated(self):
        # request.META['CSRF_COOKIE'] is a random non-empty string for
        # authed users.
        self.login()
        response = self.client.get('/', follow=True)
        # The CSRF token is a 32-character MD5 string.
        self.assertEqual(len(response._request.META['CSRF_COOKIE']), 32)

    def test_csrftoken_new_session(self):
        # The csrf_token is added to request.session the first time.
        self.login()
        response = self.client.get('/', follow=True)
        # The CSRF token is a 32-character MD5 string.
        token = response._request.session['csrf_token']
        self.assertEqual(len(token), 32)
        self.assertEqual(token, response._request.META['CSRF_COOKIE'])

    def test_csrftoken_existing_session(self):
        # The csrf_token in request.session is reused on subsequent requests.
        self.login()
        r1 = self.client.get('/', follow=True)
        token = r1._request.session['csrf_token']

        r2 = self.client.get('/', follow=True)
        self.assertEqual(r1._request.META['CSRF_COOKIE'],
                         r2._request.META['CSRF_COOKIE'])
        self.assertEqual(token, r2._request.META['CSRF_COOKIE'])

    def test_cookie_sent_when_token_used(self):
        # A CSRF cookie is sent when the CSRF token is used in a template.
        self.login()
        response = self.client.get('/token')
        self.assertEqual(response.cookies['csrftoken'].value,
                         response._request.META['CSRF_COOKIE'])

    def test_cookie_not_sent(self):
        # A CSRF cookie is not sent by default.
        self.login()
        response = self.client.get('/no-token')
        self.assertNotIn('csrftoken', response.cookies)


class TestCsrfMiddleware(django.test.TestCase):

    def setUp(self):
        self.token = 'a' * 32
        self.rf = django.test.RequestFactory()
        self.mw = CsrfMiddleware()

    def process_view(self, request, view=None):
        return self.mw.process_view(request, view, None, None)

    def test_anon_token_from_cookie(self):
        rf = django.test.RequestFactory()
        rf.cookies['anoncsrf'] = self.token
        cache.set(self.token, 'woo')
        request = rf.get('/')
        request.session = {}
        auth_mw = AuthenticationMiddleware()
        auth_mw.process_request(request)
        self.mw.process_request(request)
        self.assertEqual(request.META['CSRF_COOKIE'], 'woo')

    def test_set_csrftoken_once(self):
        # Make sure process_request only sets request.META['CSRF_COOKIE'] once.
        request = self.rf.get('/')
        request.META['CSRF_COOKIE'] = 'woo'
        self.mw.process_request(request)
        self.assertEqual(request.META['CSRF_COOKIE'], 'woo')

    def test_reject_view(self):
        # Check that the reject view returns a 403.
        response = self.process_view(self.rf.post('/'))
        self.assertEqual(response.status_code, 403)

    def test_csrf_exempt(self):
        # Make sure @csrf_exempt still works.
        view = namedtuple('_', 'csrf_exempt')
        self.assertEqual(self.process_view(self.rf.post('/'), view), None)

    def test_only_check_post(self):
        # CSRF should only get checked on POST requests.
        self.assertEqual(self.process_view(self.rf.get('/')), None)

    def test_csrfmiddlewaretoken(self):
        # The user token should be found in POST['csrfmiddlewaretoken'].
        request = self.rf.post('/', {'csrfmiddlewaretoken': self.token})
        self.assertEqual(self.process_view(request).status_code, 403)

        request.META['CSRF_COOKIE'] = self.token
        self.assertEqual(self.process_view(request), None)

    def test_x_csrftoken(self):
        # The user token can be found in the X-CSRFTOKEN header.
        request = self.rf.post('/', HTTP_X_CSRFTOKEN=self.token)
        self.assertEqual(self.process_view(request).status_code, 403)

        request.META['CSRF_COOKIE'] = self.token
        self.assertEqual(self.process_view(request), None)

    def test_require_request_token_or_user_token(self):
        # Blank request and user tokens raise an error on POST.
        request = self.rf.post('/', HTTP_X_CSRFTOKEN='')
        request.META['CSRF_COOKIE'] = ''
        self.assertEqual(self.process_view(request).status_code, 403)

    def test_token_no_match(self):
        # A 403 is returned when the tokens don't match.
        request = self.rf.post('/', HTTP_X_CSRFTOKEN='woo')
        request.META['CSRF_COOKIE'] = ''
        self.assertEqual(self.process_view(request).status_code, 403)

    def test_csrf_token_context_processor(self):
        # Our CSRF token should be available in the template context.
        request = mock.Mock()
        request.META = {'CSRF_COOKIE': self.token}
        request.groups = []
        ctx = {}
        for processor in context.get_standard_processors():
            ctx.update(processor(request))
        self.assertEqual(ctx['csrf_token'], self.token)


class TestAnonymousCsrf(django.test.TestCase):
    urls = 'session_csrf.tests'

    def setUp(self):
        self.token = 'a' * 32
        self.rf = django.test.RequestFactory()
        User.objects.create_user('jbalogh', 'j@moz.com', 'password')
        self.client.handler = ClientHandler(enforce_csrf_checks=True)
        self.save_ANON_ALWAYS = session_csrf.ANON_ALWAYS
        session_csrf.ANON_ALWAYS = False

    def tearDown(self):
        session_csrf.ANON_ALWAYS = self.save_ANON_ALWAYS

    def login(self):
        assert self.client.login(username='jbalogh', password='password')

    def test_authenticated_request(self):
        # Nothing special happens, nothing breaks.
        # Find the CSRF token in the session.
        self.login()
        response = self.client.get('/anon')
        sessionid = response.cookies['sessionid'].value
        session = Session.objects.get(session_key=sessionid)
        token = session.get_decoded()['csrf_token']

        response = self.client.post('/anon', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_request(self):
        # We get a 403 since we're not sending a token.
        response = self.client.post('/anon')
        self.assertEqual(response.status_code, 403)

    def test_no_anon_cookie(self):
        # We don't get an anon cookie on non-@anonymous_csrf views.
        response = self.client.get('/')
        self.assertEqual(response.cookies, {})

    def test_new_anon_token_on_request(self):
        # A new anon user gets a key+token on the request and response.
        response = self.client.get('/anon')
        # Get the key from the cookie and find the token in the cache.
        key = response.cookies['anoncsrf'].value
        self.assertEqual(response._request.META['CSRF_COOKIE'], cache.get(key))

    def test_existing_anon_cookie_on_request(self):
        # We reuse an existing anon cookie key+token.
        response = self.client.get('/anon')
        key = response.cookies['anoncsrf'].value

        # Now check that subsequent requests use that cookie.
        response = self.client.get('/anon')
        self.assertEqual(response.cookies['anoncsrf'].value, key)
        self.assertEqual(response._request.META['CSRF_COOKIE'], cache.get(key))

    def test_new_anon_token_on_response(self):
        # The anon cookie is sent and we vary on Cookie.
        response = self.client.get('/anon')
        self.assertIn('anoncsrf', response.cookies)
        self.assertEqual(response['Vary'], 'Cookie')

    def test_existing_anon_token_on_response(self):
        # The anon cookie is sent and we vary on Cookie, reusing the old value.
        response = self.client.get('/anon')
        key = response.cookies['anoncsrf'].value

        response = self.client.get('/anon')
        self.assertEqual(response.cookies['anoncsrf'].value, key)
        self.assertIn('anoncsrf', response.cookies)
        self.assertEqual(response['Vary'], 'Cookie')

    def test_anon_csrf_logout(self):
        # Beware of views that logout the user.
        self.login()
        response = self.client.get('/logout')
        self.assertEqual(response.status_code, 200)

    def test_existing_anon_cookie_not_in_cache(self):
        response = self.client.get('/anon')
        self.assertEqual(len(response._request.META['CSRF_COOKIE']), 32)

        # Clear cache and make sure we still get a token
        cache.clear()
        response = self.client.get('/anon')
        self.assertEqual(len(response._request.META['CSRF_COOKIE']), 32)

    def test_anonymous_csrf_exempt(self):
        response = self.client.post('/no-anon-csrf')
        self.assertEqual(response.status_code, 200)

        self.login()
        response = self.client.post('/no-anon-csrf')
        self.assertEqual(response.status_code, 403)


class TestAnonAlways(django.test.TestCase):
    # Repeats some tests with ANON_ALWAYS = True
    urls = 'session_csrf.tests'

    def setUp(self):
        self.token = 'a' * 32
        self.rf = django.test.RequestFactory()
        User.objects.create_user('jbalogh', 'j@moz.com', 'password')
        self.client.handler = ClientHandler(enforce_csrf_checks=True)
        self.save_ANON_ALWAYS = session_csrf.ANON_ALWAYS
        session_csrf.ANON_ALWAYS = True

    def tearDown(self):
        session_csrf.ANON_ALWAYS = self.save_ANON_ALWAYS

    def login(self):
        assert self.client.login(username='jbalogh', password='password')

    def test_csrftoken_unauthenticated(self):
        # request.META['CSRF_COOKIE'] is set for anonymous users
        # when ANON_ALWAYS is enabled.
        response = self.client.get('/', follow=True)
        # The CSRF token is a 32-character MD5 string.
        self.assertEqual(len(response._request.META['CSRF_COOKIE']), 32)

    def test_authenticated_request(self):
        # Nothing special happens, nothing breaks.
        # Find the CSRF token in the session.
        self.login()
        response = self.client.get('/', follow=True)
        sessionid = response.cookies['sessionid'].value
        session = Session.objects.get(session_key=sessionid)
        token = session.get_decoded()['csrf_token']

        response = self.client.post('/', follow=True, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_request(self):
        # We get a 403 since we're not sending a token.
        response = self.client.post('/')
        self.assertEqual(response.status_code, 403)

    def test_new_anon_token_on_request(self):
        # A new anon user gets a key+token on the request and response.
        response = self.client.get('/')
        # Get the key from the cookie and find the token in the cache.
        key = response.cookies['anoncsrf'].value
        self.assertEqual(response._request.META['CSRF_COOKIE'], cache.get(key))

    def test_existing_anon_cookie_on_request(self):
        # We reuse an existing anon cookie key+token.
        response = self.client.get('/')
        key = response.cookies['anoncsrf'].value

        # Now check that subsequent requests use that cookie.
        response = self.client.get('/')
        self.assertEqual(response.cookies['anoncsrf'].value, key)
        self.assertEqual(response._request.META['CSRF_COOKIE'], cache.get(key))
        self.assertEqual(response['Vary'], 'Cookie')

    def test_anon_csrf_logout(self):
        # Beware of views that logout the user.
        self.login()
        response = self.client.get('/logout')
        self.assertEqual(response.status_code, 200)

    def test_existing_anon_cookie_not_in_cache(self):
        response = self.client.get('/')
        self.assertEqual(len(response._request.META['CSRF_COOKIE']), 32)

        # Clear cache and make sure we still get a token
        cache.clear()
        response = self.client.get('/')
        self.assertEqual(len(response._request.META['CSRF_COOKIE']), 32)

    def test_csrf_cookie_secure(self):
        old_CSRF_COOKIE_SECURE = settings.CSRF_COOKIE_SECURE
        settings.CSRF_COOKIE_SECURE = True

        self.login()
        response = self.client.get('/token')
        self.assertTrue(response.cookies[settings.CSRF_COOKIE_NAME]['secure'])

        settings.CSRF_COOKIE_SECURE = old_CSRF_COOKIE_SECURE

    def test_csrf_cookie_secure_false(self):
        old_CSRF_COOKIE_SECURE = settings.CSRF_COOKIE_SECURE
        settings.CSRF_COOKIE_SECURE = False

        self.login()
        response = self.client.get('/token')
        self.assertFalse(response.cookies[settings.CSRF_COOKIE_NAME]['secure'])

        settings.CSRF_COOKIE_SECURE = old_CSRF_COOKIE_SECURE

    def test_csrf_cookie_secure_missing(self):
        old_CSRF_COOKIE_SECURE = getattr(settings, 'CSRF_COOKIE_SECURE', None)
        delattr(settings, 'CSRF_COOKIE_SECURE')

        self.login()
        resp = self.client.get('/token')
        self.assertFalse(resp.cookies[settings.CSRF_COOKIE_NAME]['secure'])

        settings.CSRF_COOKIE_SECURE = old_CSRF_COOKIE_SECURE

    def test_csrf_cookie_httponly(self):
        old_CSRF_COOKIE_HTTPONLY = getattr(settings, 'CSRF_COOKIE_HTTPONLY',
                                           None)
        settings.CSRF_COOKIE_HTTPONLY = True

        self.login()
        response = self.client.get('/token')
        self.assertTrue(response.cookies[settings.CSRF_COOKIE_NAME]['httponly'])

        settings.CSRF_COOKIE_HTTPONLY = old_CSRF_COOKIE_HTTPONLY

    def test_csrf_cookie_httponly_false(self):
        old_CSRF_COOKIE_HTTPONLY = getattr(settings, 'CSRF_COOKIE_HTTPONLY',
                                           None)
        settings.CSRF_COOKIE_HTTPONLY = False

        self.login()
        resp = self.client.get('/token')
        self.assertFalse(resp.cookies[settings.CSRF_COOKIE_NAME]['httponly'])

        settings.CSRF_COOKIE_HTTPONLY = old_CSRF_COOKIE_HTTPONLY

    def test_csrf_cookie_httponly_missing(self):
        old_CSRF_COOKIE_HTTPONLY = getattr(settings, 'CSRF_COOKIE_HTTPONLY',
                                           None)
        delattr(settings, 'CSRF_COOKIE_HTTPONLY')

        self.login()
        resp = self.client.get('/token')
        self.assertFalse(resp.cookies[settings.CSRF_COOKIE_NAME]['httponly'])

        settings.CSRF_COOKIE_HTTPONLY = old_CSRF_COOKIE_HTTPONLY


class ClientHandler(django.test.client.ClientHandler):
    """
    Handler that stores the real request object on the response.

    Almost all the code comes from the parent class.
    """

    def __call__(self, environ):
        # Set up middleware if needed. We couldn't do this earlier, because
        # settings weren't available.
        if self._request_middleware is None:
            self.load_middleware()

        signals.request_started.send(sender=self.__class__)
        try:
            request = WSGIRequest(environ)
            # sneaky little hack so that we can easily get round
            # CsrfViewMiddleware.  This makes life easier, and is probably
            # required for backwards compatibility with external tests against
            # admin views.
            request._dont_enforce_csrf_checks = not self.enforce_csrf_checks
            response = self.get_response(request)
        finally:
            signals.request_finished.disconnect(close_connection)
            signals.request_finished.send(sender=self.__class__)
            signals.request_finished.connect(close_connection)

        # Store the request object.
        response._request = request
        return response
