import base64
import os
import re
import shutil
import subprocess
import tempfile
import urllib
from contextlib import contextmanager
from datetime import timedelta
from typing import (
    Any,
    Callable,
    Collection,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)
from unittest import TestResult, mock

import lxml.html
import orjson
from django.apps import apps
from django.conf import settings
from django.core.mail import EmailMessage
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.state import StateApps
from django.db.utils import IntegrityError
from django.http import HttpRequest, HttpResponse
from django.test import TestCase
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart
from django.test.testcases import SerializeMixin
from django.urls import resolve
from django.utils import translation
from django.utils.module_loading import import_string
from django.utils.timezone import now as timezone_now
from fakeldap import MockLDAP
from two_factor.models import PhoneDevice

from corporate.models import Customer, CustomerPlan, LicenseLedger
from zerver.decorator import do_two_factor_login
from zerver.lib.actions import (
    bulk_add_subscriptions,
    bulk_remove_subscriptions,
    check_send_message,
    check_send_stream_message,
    do_set_realm_property,
    gather_subscriptions,
)
from zerver.lib.cache import bounce_key_prefix_for_testing
from zerver.lib.initial_password import initial_password
from zerver.lib.notification_data import UserMessageNotificationsData
from zerver.lib.rate_limiter import bounce_redis_key_prefix_for_testing
from zerver.lib.sessions import get_session_dict_user
from zerver.lib.stream_subscription import get_stream_subscriptions_for_user
from zerver.lib.streams import (
    create_stream_if_needed,
    get_default_value_for_history_public_to_subscribers,
)
from zerver.lib.test_console_output import (
    ExtraConsoleOutputFinder,
    ExtraConsoleOutputInTestException,
    TeeStderrAndFindExtraConsoleOutput,
    TeeStdoutAndFindExtraConsoleOutput,
)
from zerver.lib.test_helpers import find_key_by_email, instrument_url
from zerver.lib.users import get_api_key
from zerver.lib.validator import check_string
from zerver.lib.webhooks.common import (
    check_send_webhook_message,
    get_fixture_http_headers,
    standardize_headers,
)
from zerver.models import (
    Client,
    Message,
    Realm,
    Recipient,
    Stream,
    Subscription,
    UserProfile,
    clear_supported_auth_backends_cache,
    flush_per_request_caches,
    get_client,
    get_display_recipient,
    get_realm,
    get_realm_stream,
    get_stream,
    get_system_bot,
    get_user,
    get_user_by_delivery_email,
)
from zerver.openapi.openapi import validate_against_openapi_schema, validate_request
from zerver.tornado.event_queue import clear_client_event_queues_for_testing

if settings.ZILENCER_ENABLED:
    from zilencer.models import get_remote_server_by_uuid


class EmptyResponseError(Exception):
    pass


class UploadSerializeMixin(SerializeMixin):
    """
    We cannot use override_settings to change upload directory because
    because settings.LOCAL_UPLOADS_DIR is used in URL pattern and URLs
    are compiled only once. Otherwise using a different upload directory
    for conflicting test cases would have provided better performance
    while providing the required isolation.
    """

    lockfile = "var/upload_lock"

    @classmethod
    def setUpClass(cls: Any, *args: Any, **kwargs: Any) -> None:
        if not os.path.exists(cls.lockfile):
            with open(cls.lockfile, "w"):  # nocoverage - rare locking case
                pass

        super().setUpClass(*args, **kwargs)


class ZulipTestCase(TestCase):
    # Ensure that the test system just shows us diffs
    maxDiff: Optional[int] = None

    def setUp(self) -> None:
        super().setUp()
        self.API_KEYS: Dict[str, str] = {}

        test_name = self.id()
        bounce_key_prefix_for_testing(test_name)
        bounce_redis_key_prefix_for_testing(test_name)

    def tearDown(self) -> None:
        super().tearDown()
        # Important: we need to clear event queues to avoid leaking data to future tests.
        clear_client_event_queues_for_testing()
        clear_supported_auth_backends_cache()
        flush_per_request_caches()
        translation.activate(settings.LANGUAGE_CODE)

        # Clean up after using fakeldap in LDAP tests:
        if hasattr(self, "mock_ldap") and hasattr(self, "mock_initialize"):
            if self.mock_ldap is not None:
                self.mock_ldap.reset()
            self.mock_initialize.stop()

    def run(self, result: Optional[TestResult] = None) -> Optional[TestResult]:  # nocoverage
        if not settings.BAN_CONSOLE_OUTPUT:
            return super().run(result)
        extra_output_finder = ExtraConsoleOutputFinder()
        with TeeStderrAndFindExtraConsoleOutput(
            extra_output_finder
        ), TeeStdoutAndFindExtraConsoleOutput(extra_output_finder):
            test_result = super().run(result)
        if extra_output_finder.full_extra_output:
            exception_message = f"""
---- UNEXPECTED CONSOLE OUTPUT DETECTED ----

To ensure that we never miss important error output/warnings,
we require test-backend to have clean console output.

This message usually is triggered by forgotten debugging print()
statements or new logging statements.  For the latter, you can
use `with self.assertLogs()` to capture and verify the log output;
use `git grep assertLogs` to see dozens of correct examples.

You should be able to quickly reproduce this failure with:

test-backend --ban-console-output {self.id()}

Output:
{extra_output_finder.full_extra_output}
--------------------------------------------
"""
            raise ExtraConsoleOutputInTestException(exception_message)
        return test_result

    """
    WRAPPER_COMMENT:

    We wrap calls to self.client.{patch,put,get,post,delete} for various
    reasons.  Some of this has to do with fixing encodings before calling
    into the Django code.  Some of this has to do with providing a future
    path for instrumentation.  Some of it's just consistency.

    The linter will prevent direct calls to self.client.foo, so the wrapper
    functions have to fake out the linter by using a local variable called
    django_client to fool the regext.
    """
    DEFAULT_SUBDOMAIN = "zulip"
    TOKENIZED_NOREPLY_REGEX = settings.TOKENIZED_NOREPLY_EMAIL_ADDRESS.format(token="[a-z0-9_]{24}")

    def set_http_headers(self, kwargs: Dict[str, Any]) -> None:
        if "subdomain" in kwargs:
            kwargs["HTTP_HOST"] = Realm.host_for_subdomain(kwargs["subdomain"])
            del kwargs["subdomain"]
        elif "HTTP_HOST" not in kwargs:
            kwargs["HTTP_HOST"] = Realm.host_for_subdomain(self.DEFAULT_SUBDOMAIN)

        # set User-Agent
        if "HTTP_AUTHORIZATION" in kwargs:
            # An API request; use mobile as the default user agent
            default_user_agent = "ZulipMobile/26.22.145 (iOS 10.3.1)"
        else:
            # A web app request; use a browser User-Agent string.
            default_user_agent = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                + "AppleWebKit/537.36 (KHTML, like Gecko) "
                + "Chrome/79.0.3945.130 Safari/537.36"
            )
        if kwargs.get("skip_user_agent"):
            # Provide a way to disable setting User-Agent if desired.
            assert "HTTP_USER_AGENT" not in kwargs
            del kwargs["skip_user_agent"]
        elif "HTTP_USER_AGENT" not in kwargs:
            kwargs["HTTP_USER_AGENT"] = default_user_agent

    def extract_api_suffix_url(self, url: str) -> Tuple[str, Dict[str, Any]]:
        """
        Function that extracts the URL after `/api/v1` or `/json` and also
        returns the query data in the URL, if there is any.
        """
        url_split = url.split("?")
        data: Dict[str, Any] = {}
        if len(url_split) == 2:
            data = urllib.parse.parse_qs(url_split[1])
        url = url_split[0]
        url = url.replace("/json/", "/").replace("/api/v1/", "/")
        return (url, data)

    def validate_api_response_openapi(
        self,
        url: str,
        method: str,
        result: HttpResponse,
        data: Union[str, bytes, Dict[str, Any]],
        http_headers: Dict[str, Any],
        intentionally_undocumented: bool = False,
    ) -> None:
        """
        Validates all API responses received by this test against Zulip's API documentation,
        declared in zerver/openapi/zulip.yaml.  This powerful test lets us use Zulip's
        extensive test coverage of corner cases in the API to ensure that we've properly
        documented those corner cases.
        """
        if not (url.startswith("/json") or url.startswith("/api/v1")):
            return
        try:
            content = orjson.loads(result.content)
        except orjson.JSONDecodeError:
            return
        json_url = False
        if url.startswith("/json"):
            json_url = True
        url, query_data = self.extract_api_suffix_url(url)
        if len(query_data) != 0:
            # In some cases the query parameters are defined in the URL itself. In such cases
            # The `data` argument of our function is not used. Hence get `data` argument
            # from url.
            data = query_data
        response_validated = validate_against_openapi_schema(
            content, url, method, str(result.status_code)
        )
        if response_validated:
            validate_request(
                url,
                method,
                data,
                http_headers,
                json_url,
                str(result.status_code),
                intentionally_undocumented=intentionally_undocumented,
            )

    @instrument_url
    def client_patch(
        self,
        url: str,
        info: Dict[str, Any] = {},
        intentionally_undocumented: bool = False,
        **kwargs: Any,
    ) -> HttpResponse:
        """
        We need to urlencode, since Django's function won't do it for us.
        """
        encoded = urllib.parse.urlencode(info)
        django_client = self.client  # see WRAPPER_COMMENT
        self.set_http_headers(kwargs)
        result = django_client.patch(url, encoded, **kwargs)
        self.validate_api_response_openapi(
            url,
            "patch",
            result,
            info,
            kwargs,
            intentionally_undocumented=intentionally_undocumented,
        )
        return result

    @instrument_url
    def client_patch_multipart(
        self, url: str, info: Dict[str, Any] = {}, **kwargs: Any
    ) -> HttpResponse:
        """
        Use this for patch requests that have file uploads or
        that need some sort of multi-part content.  In the future
        Django's test client may become a bit more flexible,
        so we can hopefully eliminate this.  (When you post
        with the Django test client, it deals with MULTIPART_CONTENT
        automatically, but not patch.)
        """
        encoded = encode_multipart(BOUNDARY, info)
        django_client = self.client  # see WRAPPER_COMMENT
        self.set_http_headers(kwargs)
        result = django_client.patch(url, encoded, content_type=MULTIPART_CONTENT, **kwargs)
        self.validate_api_response_openapi(url, "patch", result, info, kwargs)
        return result

    @instrument_url
    def client_put(self, url: str, info: Dict[str, Any] = {}, **kwargs: Any) -> HttpResponse:
        encoded = urllib.parse.urlencode(info)
        django_client = self.client  # see WRAPPER_COMMENT
        self.set_http_headers(kwargs)
        return django_client.put(url, encoded, **kwargs)

    @instrument_url
    def client_delete(self, url: str, info: Dict[str, Any] = {}, **kwargs: Any) -> HttpResponse:
        encoded = urllib.parse.urlencode(info)
        django_client = self.client  # see WRAPPER_COMMENT
        self.set_http_headers(kwargs)
        result = django_client.delete(url, encoded, **kwargs)
        self.validate_api_response_openapi(url, "delete", result, info, kwargs)
        return result

    @instrument_url
    def client_options(self, url: str, info: Dict[str, Any] = {}, **kwargs: Any) -> HttpResponse:
        encoded = urllib.parse.urlencode(info)
        django_client = self.client  # see WRAPPER_COMMENT
        self.set_http_headers(kwargs)
        return django_client.options(url, encoded, **kwargs)

    @instrument_url
    def client_head(self, url: str, info: Dict[str, Any] = {}, **kwargs: Any) -> HttpResponse:
        encoded = urllib.parse.urlencode(info)
        django_client = self.client  # see WRAPPER_COMMENT
        self.set_http_headers(kwargs)
        return django_client.head(url, encoded, **kwargs)

    @instrument_url
    def client_post(
        self,
        url: str,
        info: Union[str, bytes, Dict[str, Any]] = {},
        **kwargs: Any,
    ) -> HttpResponse:
        intentionally_undocumented: bool = kwargs.pop("intentionally_undocumented", False)
        django_client = self.client  # see WRAPPER_COMMENT
        self.set_http_headers(kwargs)
        result = django_client.post(url, info, **kwargs)
        self.validate_api_response_openapi(
            url, "post", result, info, kwargs, intentionally_undocumented=intentionally_undocumented
        )
        return result

    @instrument_url
    def client_post_request(self, url: str, req: Any) -> HttpResponse:
        """
        We simulate hitting an endpoint here, although we
        actually resolve the URL manually and hit the view
        directly.  We have this helper method to allow our
        instrumentation to work for /notify_tornado and
        future similar methods that require doing funny
        things to a request object.
        """

        match = resolve(url)
        return match.func(req)

    @instrument_url
    def client_get(self, url: str, info: Dict[str, Any] = {}, **kwargs: Any) -> HttpResponse:
        intentionally_undocumented: bool = kwargs.pop("intentionally_undocumented", False)
        django_client = self.client  # see WRAPPER_COMMENT
        self.set_http_headers(kwargs)
        result = django_client.get(url, info, **kwargs)
        self.validate_api_response_openapi(
            url, "get", result, info, kwargs, intentionally_undocumented=intentionally_undocumented
        )
        return result

    example_user_map = dict(
        hamlet="hamlet@zulip.com",
        cordelia="cordelia@zulip.com",
        iago="iago@zulip.com",
        prospero="prospero@zulip.com",
        othello="othello@zulip.com",
        AARON="AARON@zulip.com",
        aaron="aaron@zulip.com",
        ZOE="ZOE@zulip.com",
        polonius="polonius@zulip.com",
        desdemona="desdemona@zulip.com",
        shiva="shiva@zulip.com",
        webhook_bot="webhook-bot@zulip.com",
        welcome_bot="welcome-bot@zulip.com",
        outgoing_webhook_bot="outgoing-webhook@zulip.com",
        default_bot="default-bot@zulip.com",
    )

    mit_user_map = dict(
        sipbtest="sipbtest@mit.edu",
        starnine="starnine@mit.edu",
        espuser="espuser@mit.edu",
    )

    lear_user_map = dict(
        cordelia="cordelia@zulip.com",
        king="king@lear.org",
    )

    # Non-registered test users
    nonreg_user_map = dict(
        test="test@zulip.com",
        test1="test1@zulip.com",
        alice="alice@zulip.com",
        newuser="newuser@zulip.com",
        bob="bob@zulip.com",
        cordelia="cordelia@zulip.com",
        newguy="newguy@zulip.com",
        me="me@zulip.com",
    )

    example_user_ldap_username_map = dict(
        hamlet="hamlet",
        cordelia="cordelia",
        # aaron's uid in our test directory is "letham".
        aaron="letham",
    )

    def nonreg_user(self, name: str) -> UserProfile:
        email = self.nonreg_user_map[name]
        return get_user_by_delivery_email(email, get_realm("zulip"))

    def example_user(self, name: str) -> UserProfile:
        email = self.example_user_map[name]
        return get_user_by_delivery_email(email, get_realm("zulip"))

    def mit_user(self, name: str) -> UserProfile:
        email = self.mit_user_map[name]
        return get_user(email, get_realm("zephyr"))

    def lear_user(self, name: str) -> UserProfile:
        email = self.lear_user_map[name]
        return get_user(email, get_realm("lear"))

    def nonreg_email(self, name: str) -> str:
        return self.nonreg_user_map[name]

    def example_email(self, name: str) -> str:
        return self.example_user_map[name]

    def mit_email(self, name: str) -> str:
        return self.mit_user_map[name]

    def notification_bot(self, realm: Realm) -> UserProfile:
        return get_system_bot(settings.NOTIFICATION_BOT, realm.id)

    def create_test_bot(
        self, short_name: str, user_profile: UserProfile, full_name: str = "Foo Bot", **extras: Any
    ) -> UserProfile:
        self.login_user(user_profile)
        bot_info = {
            "short_name": short_name,
            "full_name": full_name,
        }
        bot_info.update(extras)
        result = self.client_post("/json/bots", bot_info)
        self.assert_json_success(result)
        bot_email = f"{short_name}-bot@zulip.testserver"
        bot_profile = get_user(bot_email, user_profile.realm)
        return bot_profile

    def fail_to_create_test_bot(
        self,
        short_name: str,
        user_profile: UserProfile,
        full_name: str = "Foo Bot",
        *,
        assert_json_error_msg: str,
        **extras: Any,
    ) -> None:
        self.login_user(user_profile)
        bot_info = {
            "short_name": short_name,
            "full_name": full_name,
        }
        bot_info.update(extras)
        result = self.client_post("/json/bots", bot_info)
        self.assert_json_error(result, assert_json_error_msg)

    def _get_page_params(self, result: HttpResponse) -> Dict[str, Any]:
        """Helper for parsing page_params after fetching the web app's home view."""
        doc = lxml.html.document_fromstring(result.content)
        [div] = doc.xpath("//div[@id='page-params']")
        page_params_json = div.get("data-params")
        page_params = orjson.loads(page_params_json)
        return page_params

    def check_rendered_logged_in_app(self, result: HttpResponse) -> None:
        """Verifies that a visit of / was a 200 that rendered page_params
        and not for a (logged-out) spectator."""
        self.assertEqual(result.status_code, 200)
        page_params = self._get_page_params(result)
        # It is important to check `is_spectator` to verify
        # that we treated this request as a normal logged-in session,
        # not as a spectator.
        self.assertEqual(page_params["is_spectator"], False)

    def check_rendered_spectator(self, result: HttpResponse) -> None:
        """Verifies that a visit of / was a 200 that rendered page_params
        for a (logged-out) spectator."""
        self.assertEqual(result.status_code, 200)
        page_params = self._get_page_params(result)
        # It is important to check `is_spectator` to verify
        # that we treated this request to render for a `spectator`
        self.assertEqual(page_params["is_spectator"], True)

    def login_with_return(
        self, email: str, password: Optional[str] = None, **kwargs: Any
    ) -> HttpResponse:
        if password is None:
            password = initial_password(email)
        result = self.client_post(
            "/accounts/login/", {"username": email, "password": password}, **kwargs
        )
        self.assertNotEqual(result.status_code, 500)
        return result

    def login(self, name: str) -> None:
        """
        Use this for really simple tests where you just need
        to be logged in as some user, but don't need the actual
        user object for anything else.  Try to use 'hamlet' for
        non-admins and 'iago' for admins:

            self.login('hamlet')

        Try to use 'cordelia' or 'othello' as "other" users.
        """
        assert "@" not in name, "use login_by_email for email logins"
        user = self.example_user(name)
        self.login_user(user)

    def login_by_email(self, email: str, password: str) -> None:
        realm = get_realm("zulip")
        request = HttpRequest()
        request.session = self.client.session
        self.assertTrue(
            self.client.login(
                request=request,
                username=email,
                password=password,
                realm=realm,
            ),
        )

    def assert_login_failure(self, email: str, password: str) -> None:
        realm = get_realm("zulip")
        self.assertFalse(
            self.client.login(
                username=email,
                password=password,
                realm=realm,
            ),
        )

    def login_user(self, user_profile: UserProfile) -> None:
        email = user_profile.delivery_email
        realm = user_profile.realm
        password = initial_password(email)
        request = HttpRequest()
        request.session = self.client.session
        self.assertTrue(
            self.client.login(request=request, username=email, password=password, realm=realm)
        )

    def login_2fa(self, user_profile: UserProfile) -> None:
        """
        We need this function to call request.session.save().
        do_two_factor_login doesn't save session; in normal request-response
        cycle this doesn't matter because middleware will save the session
        when it finds it dirty; however,in tests we will have to do that
        explicitly.
        """
        request = HttpRequest()
        request.session = self.client.session
        request.user = user_profile
        do_two_factor_login(request, user_profile)
        request.session.save()

    def logout(self) -> None:
        self.client.logout()

    def register(self, email: str, password: str, **kwargs: Any) -> HttpResponse:
        self.client_post("/accounts/home/", {"email": email}, **kwargs)
        return self.submit_reg_form_for_user(email, password, **kwargs)

    def submit_reg_form_for_user(
        self,
        email: str,
        password: Optional[str],
        realm_name: str = "Zulip Test",
        realm_subdomain: str = "zuliptest",
        from_confirmation: str = "",
        full_name: Optional[str] = None,
        timezone: str = "",
        realm_in_root_domain: Optional[str] = None,
        default_stream_groups: Sequence[str] = [],
        source_realm_id: str = "",
        key: Optional[str] = None,
        realm_type: Optional[int] = Realm.ORG_TYPES["business"]["id"],
        **kwargs: Any,
    ) -> HttpResponse:
        """
        Stage two of the two-step registration process.

        If things are working correctly the account should be fully
        registered after this call.

        You can pass the HTTP_HOST variable for subdomains via kwargs.
        """
        if full_name is None:
            full_name = email.replace("@", "_")
        payload = {
            "full_name": full_name,
            "realm_name": realm_name,
            "realm_subdomain": realm_subdomain,
            "realm_type": realm_type,
            "key": key if key is not None else find_key_by_email(email),
            "timezone": timezone,
            "terms": True,
            "from_confirmation": from_confirmation,
            "default_stream_group": default_stream_groups,
            "source_realm_id": source_realm_id,
        }
        if password is not None:
            payload["password"] = password
        if realm_in_root_domain is not None:
            payload["realm_in_root_domain"] = realm_in_root_domain
        return self.client_post("/accounts/register/", payload, **kwargs)

    def get_confirmation_url_from_outbox(
        self,
        email_address: str,
        *,
        url_pattern: Optional[str] = None,
        email_subject_contains: Optional[str] = None,
        email_body_contains: Optional[str] = None,
    ) -> str:
        from django.core.mail import outbox

        if url_pattern is None:
            # This is a bit of a crude heuristic, but good enough for most tests.
            url_pattern = settings.EXTERNAL_HOST + r"(\S+)>"
        for message in reversed(outbox):
            if any(
                addr == email_address or addr.endswith(f" <{email_address}>") for addr in message.to
            ):
                match = re.search(url_pattern, message.body)
                assert match is not None

                if email_subject_contains:
                    self.assertIn(email_subject_contains, message.subject)

                if email_body_contains:
                    self.assertIn(email_body_contains, message.body)

                [confirmation_url] = match.groups()
                return confirmation_url
        else:
            raise AssertionError("Couldn't find a confirmation email.")

    def encode_uuid(self, uuid: str) -> str:
        """
        identifier: Can be an email or a remote server uuid.
        """
        if uuid in self.API_KEYS:
            api_key = self.API_KEYS[uuid]
        else:
            api_key = get_remote_server_by_uuid(uuid).api_key
            self.API_KEYS[uuid] = api_key

        return self.encode_credentials(uuid, api_key)

    def encode_user(self, user: UserProfile) -> str:
        email = user.delivery_email
        api_key = user.api_key
        return self.encode_credentials(email, api_key)

    def encode_email(self, email: str, realm: str = "zulip") -> str:
        # TODO: use encode_user where possible
        assert "@" in email
        user = get_user_by_delivery_email(email, get_realm(realm))
        api_key = get_api_key(user)

        return self.encode_credentials(email, api_key)

    def encode_credentials(self, identifier: str, api_key: str) -> str:
        """
        identifier: Can be an email or a remote server uuid.
        """
        credentials = f"{identifier}:{api_key}"
        return "Basic " + base64.b64encode(credentials.encode("utf-8")).decode("utf-8")

    def uuid_get(self, identifier: str, *args: Any, **kwargs: Any) -> HttpResponse:
        kwargs["HTTP_AUTHORIZATION"] = self.encode_uuid(identifier)
        return self.client_get(*args, **kwargs)

    def uuid_post(self, identifier: str, *args: Any, **kwargs: Any) -> HttpResponse:
        kwargs["HTTP_AUTHORIZATION"] = self.encode_uuid(identifier)
        return self.client_post(*args, **kwargs)

    def api_get(self, user: UserProfile, *args: Any, **kwargs: Any) -> HttpResponse:
        kwargs["HTTP_AUTHORIZATION"] = self.encode_user(user)
        return self.client_get(*args, **kwargs)

    def api_post(
        self, user: UserProfile, *args: Any, intentionally_undocumented: bool = False, **kwargs: Any
    ) -> HttpResponse:
        kwargs["HTTP_AUTHORIZATION"] = self.encode_user(user)
        return self.client_post(
            *args, intentionally_undocumented=intentionally_undocumented, **kwargs
        )

    def api_patch(self, user: UserProfile, *args: Any, **kwargs: Any) -> HttpResponse:
        kwargs["HTTP_AUTHORIZATION"] = self.encode_user(user)
        return self.client_patch(*args, **kwargs)

    def api_delete(self, user: UserProfile, *args: Any, **kwargs: Any) -> HttpResponse:
        kwargs["HTTP_AUTHORIZATION"] = self.encode_user(user)
        return self.client_delete(*args, **kwargs)

    def get_streams(self, user_profile: UserProfile) -> List[str]:
        """
        Helper function to get the stream names for a user
        """
        subs = get_stream_subscriptions_for_user(user_profile).filter(
            active=True,
        )
        return [check_string("recipient", get_display_recipient(sub.recipient)) for sub in subs]

    def send_personal_message(
        self,
        from_user: UserProfile,
        to_user: UserProfile,
        content: str = "test content",
        sending_client_name: str = "test suite",
    ) -> int:
        recipient_list = [to_user.id]
        (sending_client, _) = Client.objects.get_or_create(name=sending_client_name)

        return check_send_message(
            from_user,
            sending_client,
            "private",
            recipient_list,
            None,
            content,
        )

    def send_huddle_message(
        self,
        from_user: UserProfile,
        to_users: List[UserProfile],
        content: str = "test content",
        sending_client_name: str = "test suite",
    ) -> int:
        to_user_ids = [u.id for u in to_users]
        assert len(to_user_ids) >= 2

        (sending_client, _) = Client.objects.get_or_create(name=sending_client_name)

        return check_send_message(
            from_user,
            sending_client,
            "private",
            to_user_ids,
            None,
            content,
        )

    def send_stream_message(
        self,
        sender: UserProfile,
        stream_name: str,
        content: str = "test content",
        topic_name: str = "test",
        recipient_realm: Optional[Realm] = None,
        sending_client_name: str = "test suite",
    ) -> int:
        (sending_client, _) = Client.objects.get_or_create(name=sending_client_name)

        return check_send_stream_message(
            sender=sender,
            client=sending_client,
            stream_name=stream_name,
            topic=topic_name,
            body=content,
            realm=recipient_realm,
        )

    def get_messages_response(
        self,
        anchor: Union[int, str] = 1,
        num_before: int = 100,
        num_after: int = 100,
        use_first_unread_anchor: bool = False,
    ) -> Dict[str, List[Dict[str, Any]]]:
        post_params = {
            "anchor": anchor,
            "num_before": num_before,
            "num_after": num_after,
            "use_first_unread_anchor": orjson.dumps(use_first_unread_anchor).decode(),
        }
        result = self.client_get("/json/messages", dict(post_params))
        data = result.json()
        return data

    def get_messages(
        self,
        anchor: Union[str, int] = 1,
        num_before: int = 100,
        num_after: int = 100,
        use_first_unread_anchor: bool = False,
    ) -> List[Dict[str, Any]]:
        data = self.get_messages_response(anchor, num_before, num_after, use_first_unread_anchor)
        return data["messages"]

    def users_subscribed_to_stream(self, stream_name: str, realm: Realm) -> List[UserProfile]:
        stream = Stream.objects.get(name=stream_name, realm=realm)
        recipient = Recipient.objects.get(type_id=stream.id, type=Recipient.STREAM)
        subscriptions = Subscription.objects.filter(recipient=recipient, active=True)

        return [subscription.user_profile for subscription in subscriptions]

    def assert_url_serves_contents_of_file(self, url: str, result: bytes) -> None:
        response = self.client_get(url)
        data = b"".join(response.streaming_content)
        self.assertEqual(result, data)

    def assert_json_success(self, result: HttpResponse) -> Dict[str, Any]:
        """
        Successful POSTs return a 200 and JSON of the form {"result": "success",
        "msg": ""}.
        """
        try:
            json = orjson.loads(result.content)
        except orjson.JSONDecodeError:  # nocoverage
            json = {"msg": "Error parsing JSON in response!"}
        self.assertEqual(result.status_code, 200, json["msg"])
        self.assertEqual(json.get("result"), "success")
        # We have a msg key for consistency with errors, but it typically has an
        # empty value.
        self.assertIn("msg", json)
        self.assertNotEqual(json["msg"], "Error parsing JSON in response!")
        return json

    def get_json_error(self, result: HttpResponse, status_code: int = 400) -> str:
        try:
            json = orjson.loads(result.content)
        except orjson.JSONDecodeError:  # nocoverage
            json = {"msg": "Error parsing JSON in response!"}
        self.assertEqual(result.status_code, status_code, msg=json.get("msg"))
        self.assertEqual(json.get("result"), "error")
        return json["msg"]

    def assert_json_error(self, result: HttpResponse, msg: str, status_code: int = 400) -> None:
        """
        Invalid POSTs return an error status code and JSON of the form
        {"result": "error", "msg": "reason"}.
        """
        self.assertEqual(self.get_json_error(result, status_code=status_code), msg)

    def assert_length(self, items: Collection[Any], count: int) -> None:
        actual_count = len(items)
        if actual_count != count:  # nocoverage
            print("\nITEMS:\n")
            for item in items:
                print(item)
            print(f"\nexpected length: {count}\nactual length: {actual_count}")
            raise AssertionError(f"{str(type(items))} is of unexpected size!")

    def assert_json_error_contains(
        self, result: HttpResponse, msg_substring: str, status_code: int = 400
    ) -> None:
        self.assertIn(msg_substring, self.get_json_error(result, status_code=status_code))

    def assert_in_response(self, substring: str, response: HttpResponse) -> None:
        self.assertIn(substring, response.content.decode("utf-8"))

    def assert_in_success_response(self, substrings: List[str], response: HttpResponse) -> None:
        self.assertEqual(response.status_code, 200)
        decoded = response.content.decode("utf-8")
        for substring in substrings:
            self.assertIn(substring, decoded)

    def assert_not_in_success_response(self, substrings: List[str], response: HttpResponse) -> None:
        self.assertEqual(response.status_code, 200)
        decoded = response.content.decode("utf-8")
        for substring in substrings:
            self.assertNotIn(substring, decoded)

    def assert_logged_in_user_id(self, user_id: Optional[int]) -> None:
        """
        Verifies the user currently logged in for the test client has the provided user_id.
        Pass None to verify no user is logged in.
        """
        self.assertEqual(get_session_dict_user(self.client.session), user_id)

    def webhook_fixture_data(self, type: str, action: str, file_type: str = "json") -> str:
        fn = os.path.join(
            os.path.dirname(__file__),
            f"../webhooks/{type}/fixtures/{action}.{file_type}",
        )
        with open(fn) as f:
            return f.read()

    def fixture_file_name(self, file_name: str, type: str = "") -> str:
        return os.path.join(
            os.path.dirname(__file__),
            f"../tests/fixtures/{type}/{file_name}",
        )

    def fixture_data(self, file_name: str, type: str = "") -> str:
        fn = self.fixture_file_name(file_name, type)
        with open(fn) as f:
            return f.read()

    def make_stream(
        self,
        stream_name: str,
        realm: Optional[Realm] = None,
        invite_only: bool = False,
        is_web_public: bool = False,
        history_public_to_subscribers: Optional[bool] = None,
    ) -> Stream:
        if realm is None:
            realm = get_realm("zulip")

        history_public_to_subscribers = get_default_value_for_history_public_to_subscribers(
            realm, invite_only, history_public_to_subscribers
        )

        try:
            stream = Stream.objects.create(
                realm=realm,
                name=stream_name,
                invite_only=invite_only,
                is_web_public=is_web_public,
                history_public_to_subscribers=history_public_to_subscribers,
            )
        except IntegrityError:  # nocoverage -- this is for bugs in the tests
            raise Exception(
                f"""
                {stream_name} already exists

                Please call make_stream with a stream name
                that is not already in use."""
            )

        recipient = Recipient.objects.create(type_id=stream.id, type=Recipient.STREAM)
        stream.recipient = recipient
        stream.save(update_fields=["recipient"])
        return stream

    INVALID_STREAM_ID = 999999

    def get_stream_id(self, name: str, realm: Optional[Realm] = None) -> int:
        if not realm:
            realm = get_realm("zulip")
        try:
            stream = get_realm_stream(name, realm.id)
        except Stream.DoesNotExist:
            return self.INVALID_STREAM_ID
        return stream.id

    # Subscribe to a stream directly
    def subscribe(self, user_profile: UserProfile, stream_name: str) -> Stream:
        realm = user_profile.realm
        try:
            stream = get_stream(stream_name, user_profile.realm)
        except Stream.DoesNotExist:
            stream, from_stream_creation = create_stream_if_needed(realm, stream_name)
        bulk_add_subscriptions(realm, [stream], [user_profile], acting_user=None)
        return stream

    def unsubscribe(self, user_profile: UserProfile, stream_name: str) -> None:
        client = get_client("website")
        stream = get_stream(stream_name, user_profile.realm)
        bulk_remove_subscriptions([user_profile], [stream], client, acting_user=None)

    # Subscribe to a stream by making an API request
    def common_subscribe_to_streams(
        self,
        user: UserProfile,
        streams: Iterable[str],
        extra_post_data: Dict[str, Any] = {},
        invite_only: bool = False,
        is_web_public: bool = False,
        allow_fail: bool = False,
        **kwargs: Any,
    ) -> HttpResponse:
        post_data = {
            "subscriptions": orjson.dumps([{"name": stream} for stream in streams]).decode(),
            "is_web_public": orjson.dumps(is_web_public).decode(),
            "invite_only": orjson.dumps(invite_only).decode(),
        }
        post_data.update(extra_post_data)
        result = self.api_post(user, "/api/v1/users/me/subscriptions", post_data, **kwargs)
        if not allow_fail:
            self.assert_json_success(result)
        return result

    def check_user_subscribed_only_to_streams(self, user_name: str, streams: List[Stream]) -> None:
        streams = sorted(streams, key=lambda x: x.name)
        subscribed_streams = gather_subscriptions(self.nonreg_user(user_name))[0]

        self.assert_length(subscribed_streams, len(streams))

        for x, y in zip(subscribed_streams, streams):
            self.assertEqual(x["name"], y.name)

    def send_webhook_payload(
        self,
        user_profile: UserProfile,
        url: str,
        payload: Union[str, Dict[str, Any]],
        **post_params: Any,
    ) -> Message:
        """
        Send a webhook payload to the server, and verify that the
        post is successful.

        This is a pretty low-level function.  For most use cases
        see the helpers that call this function, which do additional
        checks.

        Occasionally tests will call this directly, for unique
        situations like having multiple messages go to a stream,
        where the other helper functions are a bit too rigid,
        and you'll want the test itself do various assertions.
        Even in those cases, you're often better to simply
        call client_post and assert_json_success.

        If the caller expects a message to be sent to a stream,
        the caller should make sure the user is subscribed.
        """

        prior_msg = self.get_last_message()

        result = self.client_post(url, payload, **post_params)
        self.assert_json_success(result)

        # Check the correct message was sent
        msg = self.get_last_message()

        if msg.id == prior_msg.id:
            raise EmptyResponseError(
                """
                Your test code called an endpoint that did
                not write any new messages.  It is probably
                broken (but still returns 200 due to exception
                handling).

                One possible gotcha is that you forgot to
                subscribe the test user to the stream that
                the webhook sends to.
                """
            )  # nocoverage

        self.assertEqual(msg.sender.email, user_profile.email)

        return msg

    def get_last_message(self) -> Message:
        return Message.objects.latest("id")

    def get_second_to_last_message(self) -> Message:
        return Message.objects.all().order_by("-id")[1]

    @contextmanager
    def simulated_markdown_failure(self) -> Iterator[None]:
        """
        This raises a failure inside of the try/except block of
        markdown.__init__.do_convert.
        """
        with self.settings(ERROR_BOT=None), mock.patch(
            "zerver.lib.markdown.timeout", side_effect=subprocess.CalledProcessError(1, [])
        ), self.assertLogs(
            level="ERROR"
        ):  # For markdown_logger.exception
            yield

    def create_default_device(
        self, user_profile: UserProfile, number: str = "+12125550100"
    ) -> None:
        phone_device = PhoneDevice(
            user=user_profile,
            name="default",
            confirmed=True,
            number=number,
            key="abcd",
            method="sms",
        )
        phone_device.save()

    def rm_tree(self, path: str) -> None:
        if os.path.exists(path):
            shutil.rmtree(path)

    def make_import_output_dir(self, exported_from: str) -> str:
        output_dir = tempfile.mkdtemp(
            dir=settings.TEST_WORKER_DIR, prefix="test-" + exported_from + "-import-"
        )
        os.makedirs(output_dir, exist_ok=True)
        return output_dir

    def get_set(self, data: List[Dict[str, Any]], field: str) -> Set[str]:
        values = {r[field] for r in data}
        return values

    def find_by_id(self, data: List[Dict[str, Any]], db_id: int) -> Dict[str, Any]:
        return [r for r in data if r["id"] == db_id][0]

    def init_default_ldap_database(self) -> None:
        """
        Takes care of the mock_ldap setup, loads
        a directory from zerver/tests/fixtures/ldap/directory.json with various entries
        to be used by tests.
        If a test wants to specify its own directory, it can just replace
        self.mock_ldap.directory with its own content, but in most cases it should be
        enough to use change_user_attr to make simple modifications to the pre-loaded
        directory. If new user entries are needed to test for some additional unusual
        scenario, it's most likely best to add that to directory.json.
        """
        directory = orjson.loads(self.fixture_data("directory.json", type="ldap"))

        for dn, attrs in directory.items():
            if "uid" in attrs:
                # Generate a password for the LDAP account:
                attrs["userPassword"] = [self.ldap_password(attrs["uid"][0])]

            # Load binary attributes. If in "directory", an attribute as its value
            # has a string starting with "file:", the rest of the string is assumed
            # to be a path to the file from which binary data should be loaded,
            # as the actual value of the attribute in LDAP.
            for attr, value in attrs.items():
                if isinstance(value, str) and value.startswith("file:"):
                    with open(value[5:], "rb") as f:
                        attrs[attr] = [f.read()]

        ldap_patcher = mock.patch("django_auth_ldap.config.ldap.initialize")
        self.mock_initialize = ldap_patcher.start()
        self.mock_ldap = MockLDAP(directory)
        self.mock_initialize.return_value = self.mock_ldap

    def change_ldap_user_attr(
        self, username: str, attr_name: str, attr_value: Union[str, bytes], binary: bool = False
    ) -> None:
        """
        Method for changing the value of an attribute of a user entry in the mock
        directory. Use option binary=True if you want binary data to be loaded
        into the attribute from a file specified at attr_value. This changes
        the attribute only for the specific test function that calls this method,
        and is isolated from other tests.
        """
        dn = f"uid={username},ou=users,dc=zulip,dc=com"
        if binary:
            with open(attr_value, "rb") as f:
                # attr_value should be a path to the file with the binary data
                data: Union[str, bytes] = f.read()
        else:
            data = attr_value

        self.mock_ldap.directory[dn][attr_name] = [data]

    def remove_ldap_user_attr(self, username: str, attr_name: str) -> None:
        """
        Method for removing the value of an attribute of a user entry in the mock
        directory. This changes the attribute only for the specific test function
        that calls this method, and is isolated from other tests.
        """
        dn = f"uid={username},ou=users,dc=zulip,dc=com"
        self.mock_ldap.directory[dn].pop(attr_name, None)

    def ldap_username(self, username: str) -> str:
        """
        Maps Zulip username to the name of the corresponding LDAP user
        in our test directory at zerver/tests/fixtures/ldap/directory.json,
        if the LDAP user exists.
        """
        return self.example_user_ldap_username_map[username]

    def ldap_password(self, uid: str) -> str:
        return f"{uid}_ldap_password"

    def email_display_from(self, email_message: EmailMessage) -> str:
        """
        Returns the email address that will show in email clients as the
        "From" field.
        """
        # The extra_headers field may contain a "From" which is used
        # for display in email clients, and appears in the RFC822
        # header as `From`.  The `.from_email` accessor is the
        # "envelope from" address, used by mail transfer agents if
        # the email bounces.
        return email_message.extra_headers.get("From", email_message.from_email)

    def email_envelope_from(self, email_message: EmailMessage) -> str:
        """
        Returns the email address that will be used if the email bounces.
        """
        # See email_display_from, above.
        return email_message.from_email

    def check_has_permission_policies(
        self, policy: str, validation_func: Callable[[UserProfile], bool]
    ) -> None:

        realm = get_realm("zulip")
        admin_user = self.example_user("iago")
        moderator_user = self.example_user("shiva")
        member_user = self.example_user("hamlet")
        new_member_user = self.example_user("othello")
        guest_user = self.example_user("polonius")

        do_set_realm_property(realm, "waiting_period_threshold", 1000, acting_user=None)
        new_member_user.date_joined = timezone_now() - timedelta(
            days=(realm.waiting_period_threshold - 1)
        )
        new_member_user.save()

        member_user.date_joined = timezone_now() - timedelta(
            days=(realm.waiting_period_threshold + 1)
        )
        member_user.save()

        do_set_realm_property(realm, policy, Realm.POLICY_ADMINS_ONLY, acting_user=None)
        self.assertTrue(validation_func(admin_user))
        self.assertFalse(validation_func(moderator_user))
        self.assertFalse(validation_func(member_user))
        self.assertFalse(validation_func(new_member_user))
        self.assertFalse(validation_func(guest_user))

        do_set_realm_property(realm, policy, Realm.POLICY_MODERATORS_ONLY, acting_user=None)
        self.assertTrue(validation_func(admin_user))
        self.assertTrue(validation_func(moderator_user))
        self.assertFalse(validation_func(member_user))
        self.assertFalse(validation_func(new_member_user))
        self.assertFalse(validation_func(guest_user))

        do_set_realm_property(realm, policy, Realm.POLICY_FULL_MEMBERS_ONLY, acting_user=None)
        self.assertTrue(validation_func(admin_user))
        self.assertTrue(validation_func(moderator_user))
        self.assertTrue(validation_func(member_user))
        self.assertFalse(validation_func(new_member_user))
        self.assertFalse(validation_func(guest_user))

        do_set_realm_property(realm, policy, Realm.POLICY_MEMBERS_ONLY, acting_user=None)
        self.assertTrue(validation_func(admin_user))
        self.assertTrue(validation_func(moderator_user))
        self.assertTrue(validation_func(member_user))
        self.assertTrue(validation_func(new_member_user))
        self.assertFalse(validation_func(guest_user))

    def subscribe_realm_to_manual_license_management_plan(
        self, realm: Realm, licenses: int, licenses_at_next_renewal: int, billing_schedule: int
    ) -> Tuple[CustomerPlan, LicenseLedger]:
        customer, _ = Customer.objects.get_or_create(realm=realm)
        plan = CustomerPlan.objects.create(
            customer=customer,
            automanage_licenses=False,
            billing_cycle_anchor=timezone_now(),
            billing_schedule=billing_schedule,
            tier=CustomerPlan.STANDARD,
        )
        ledger = LicenseLedger.objects.create(
            plan=plan,
            is_renewal=True,
            event_time=timezone_now(),
            licenses=licenses,
            licenses_at_next_renewal=licenses_at_next_renewal,
        )
        realm.plan_type = Realm.STANDARD
        realm.save(update_fields=["plan_type"])
        return plan, ledger

    def subscribe_realm_to_monthly_plan_on_manual_license_management(
        self, realm: Realm, licenses: int, licenses_at_next_renewal: int
    ) -> Tuple[CustomerPlan, LicenseLedger]:
        return self.subscribe_realm_to_manual_license_management_plan(
            realm, licenses, licenses_at_next_renewal, CustomerPlan.MONTHLY
        )

    @contextmanager
    def tornado_redirected_to_list(
        self, lst: List[Mapping[str, Any]], expected_num_events: int
    ) -> Iterator[None]:
        lst.clear()

        # process_notification takes a single parameter called 'notice'.
        # lst.append takes a single argument called 'object'.
        # Some code might call process_notification using keyword arguments,
        # so mypy doesn't allow assigning lst.append to process_notification
        # So explicitly change parameter name to 'notice' to work around this problem
        with mock.patch(
            "zerver.tornado.django_api.process_notification", lambda notice: lst.append(notice)
        ):
            # Some `send_event` calls need to be executed only after the current transaction
            # commits (using `on_commit` hooks). Because the transaction in Django tests never
            # commits (rather, gets rolled back after the test completes), such events would
            # never be sent in tests, and we would be unable to verify them. Hence, we use
            # this helper to make sure the `send_event` calls actually run.
            with self.captureOnCommitCallbacks(execute=True):
                yield

        self.assert_length(lst, expected_num_events)

    def create_user_notifications_data_object(
        self, *, user_id: int, **kwargs: Any
    ) -> UserMessageNotificationsData:
        return UserMessageNotificationsData(
            user_id=user_id,
            online_push_enabled=kwargs.get("online_push_enabled", False),
            pm_email_notify=kwargs.get("pm_email_notify", False),
            pm_push_notify=kwargs.get("pm_push_notify", False),
            mention_email_notify=kwargs.get("mention_email_notify", False),
            mention_push_notify=kwargs.get("mention_push_notify", False),
            stream_email_notify=kwargs.get("stream_email_notify", False),
            stream_push_notify=kwargs.get("stream_push_notify", False),
            wildcard_mention_notify=kwargs.get("wildcard_mention_notify", False),
            sender_is_muted=kwargs.get("sender_is_muted", False),
        )

    def get_maybe_enqueue_notifications_parameters(
        self, *, message_id: int, user_id: int, acting_user_id: int, **kwargs: Any
    ) -> Dict[str, Any]:
        """
        Returns a dictionary with the passed parameters, after filling up the
        missing data with default values, for testing what was passed to the
        `maybe_enqueue_notifications` method.
        """
        user_notifications_data = self.create_user_notifications_data_object(
            user_id=user_id, **kwargs
        )
        return dict(
            user_notifications_data=user_notifications_data,
            message_id=message_id,
            acting_user_id=acting_user_id,
            mentioned_user_group_id=kwargs.get("mentioned_user_group_id", None),
            idle=kwargs.get("idle", True),
            already_notified=kwargs.get(
                "already_notified", {"email_notified": False, "push_notified": False}
            ),
        )


class WebhookTestCase(ZulipTestCase):
    """Shared test class for all incoming webhooks tests.

    Used by configuring the below class attributes, and calling
    send_and_test_message in individual tests.

    * Tests can override build_webhook_url if the webhook requires a
      different URL format.

    * Tests can override get_body for cases where there is no
      available fixture file.

    * Tests should specify WEBHOOK_DIR_NAME to enforce that all event
      types are declared in the @webhook_view decorator. This is
      important for ensuring we document all fully supported event types.
    """

    STREAM_NAME: Optional[str] = None
    TEST_USER_EMAIL = "webhook-bot@zulip.com"
    URL_TEMPLATE: str
    WEBHOOK_DIR_NAME: Optional[str] = None
    # This last parameter is a workaround to handle webhooks that do not
    # name the main function api_{WEBHOOK_DIR_NAME}_webhook.
    VIEW_FUNCTION_NAME: Optional[str] = None

    @property
    def test_user(self) -> UserProfile:
        return get_user(self.TEST_USER_EMAIL, get_realm("zulip"))

    def setUp(self) -> None:
        super().setUp()
        self.url = self.build_webhook_url()

        if self.WEBHOOK_DIR_NAME is not None:
            # If VIEW_FUNCTION_NAME is explicitly specified and
            # WEBHOOK_DIR_NAME is not None, an exception will be
            # raised when a test triggers events that are not
            # explicitly specified via the event_types parameter to
            # the @webhook_view decorator.
            if self.VIEW_FUNCTION_NAME is None:
                function = import_string(
                    f"zerver.webhooks.{self.WEBHOOK_DIR_NAME}.view.api_{self.WEBHOOK_DIR_NAME}_webhook"
                )
            else:
                function = import_string(
                    f"zerver.webhooks.{self.WEBHOOK_DIR_NAME}.view.{self.VIEW_FUNCTION_NAME}"
                )
            all_event_types = None

            if hasattr(function, "_all_event_types"):
                all_event_types = function._all_event_types

            if all_event_types is None:
                return  # nocoverage

            def side_effect(*args: Any, **kwargs: Any) -> None:
                complete_event_type = (
                    kwargs.get("complete_event_type")
                    if len(args) < 5
                    else args[4]  # complete_event_type is the argument at index 4
                )
                if (
                    complete_event_type is not None
                    and all_event_types is not None
                    and complete_event_type not in all_event_types
                ):
                    raise Exception(
                        f"""
Error: This test triggered a message using the event "{complete_event_type}", which was not properly
registered via the @webhook_view(..., event_types=[...]). These registrations are important for Zulip
self-documenting the supported event types for this integration.

You can fix this by adding "{complete_event_type}" to ALL_EVENT_TYPES for this webhook.
""".strip()
                    )
                check_send_webhook_message(*args, **kwargs)

            self.patch = mock.patch(
                f"zerver.webhooks.{self.WEBHOOK_DIR_NAME}.view.check_send_webhook_message",
                side_effect=side_effect,
            )
            self.patch.start()
            self.addCleanup(self.patch.stop)

    def api_stream_message(self, user: UserProfile, *args: Any, **kwargs: Any) -> HttpResponse:
        kwargs["HTTP_AUTHORIZATION"] = self.encode_user(user)
        return self.check_webhook(*args, **kwargs)

    def check_webhook(
        self,
        fixture_name: str,
        expected_topic: Optional[str] = None,
        expected_message: Optional[str] = None,
        content_type: Optional[str] = "application/json",
        expect_noop: Optional[bool] = False,
        **kwargs: Any,
    ) -> None:
        """
        check_webhook is the main way to test "normal" webhooks that
        work by receiving a payload from a third party and then writing
        some message to a Zulip stream.

        We use `fixture_name` to find the payload data in of our test
        fixtures.  Then we verify that a message gets sent to a stream:

            self.STREAM_NAME: stream name
            expected_topic: topic
            expected_message: content

        We simulate the delivery of the payload with `content_type`,
        and you can pass other headers via `kwargs`.

        For the rare cases of webhooks actually sending private messages,
        see send_and_test_private_message.

        When no message is expected to be sent, set `expect_noop` to True.
        """
        assert self.STREAM_NAME is not None
        self.subscribe(self.test_user, self.STREAM_NAME)

        payload = self.get_payload(fixture_name)
        if content_type is not None:
            kwargs["content_type"] = content_type
        if self.WEBHOOK_DIR_NAME is not None:
            headers = get_fixture_http_headers(self.WEBHOOK_DIR_NAME, fixture_name)
            headers = standardize_headers(headers)
            kwargs.update(headers)
        try:
            msg = self.send_webhook_payload(
                self.test_user,
                self.url,
                payload,
                **kwargs,
            )
        except EmptyResponseError:
            if expect_noop:
                return
            else:
                raise AssertionError(
                    "No message was sent. Pass expect_noop=True if this is intentional."
                )

        if expect_noop:
            raise Exception(
                """
While no message is expected given expect_noop=True,
your test code triggered an endpoint that did write
one or more new messages.
""".strip()
            )
        assert expected_message is not None and expected_topic is not None

        self.assert_stream_message(
            message=msg,
            stream_name=self.STREAM_NAME,
            topic_name=expected_topic,
            content=expected_message,
        )

    def assert_stream_message(
        self,
        message: Message,
        stream_name: str,
        topic_name: str,
        content: str,
    ) -> None:
        self.assertEqual(get_display_recipient(message.recipient), stream_name)
        self.assertEqual(message.topic_name(), topic_name)
        self.assertEqual(message.content, content)

    def send_and_test_private_message(
        self,
        fixture_name: str,
        expected_message: str,
        content_type: str = "application/json",
        **kwargs: Any,
    ) -> Message:
        """
        For the rare cases that you are testing a webhook that sends
        private messages, use this function.

        Most webhooks send to streams, and you will want to look at
        check_webhook.
        """
        payload = self.get_payload(fixture_name)
        kwargs["content_type"] = content_type

        if self.WEBHOOK_DIR_NAME is not None:
            headers = get_fixture_http_headers(self.WEBHOOK_DIR_NAME, fixture_name)
            headers = standardize_headers(headers)
            kwargs.update(headers)
        # The sender profile shouldn't be passed any further in kwargs, so we pop it.
        sender = kwargs.pop("sender", self.test_user)

        msg = self.send_webhook_payload(
            sender,
            self.url,
            payload,
            **kwargs,
        )
        self.assertEqual(msg.content, expected_message)

        return msg

    def build_webhook_url(self, *args: Any, **kwargs: Any) -> str:
        url = self.URL_TEMPLATE
        if url.find("api_key") >= 0:
            api_key = get_api_key(self.test_user)
            url = self.URL_TEMPLATE.format(api_key=api_key, stream=self.STREAM_NAME)
        else:
            url = self.URL_TEMPLATE.format(stream=self.STREAM_NAME)

        has_arguments = kwargs or args
        if has_arguments and url.find("?") == -1:
            url = f"{url}?"  # nocoverage
        else:
            url = f"{url}&"

        for key, value in kwargs.items():
            url = f"{url}{key}={value}&"

        for arg in args:
            url = f"{url}{arg}&"

        return url[:-1] if has_arguments else url

    def get_payload(self, fixture_name: str) -> Union[str, Dict[str, str]]:
        """
        Generally webhooks that override this should return dicts."""
        return self.get_body(fixture_name)

    def get_body(self, fixture_name: str) -> str:
        assert self.WEBHOOK_DIR_NAME is not None
        body = self.webhook_fixture_data(self.WEBHOOK_DIR_NAME, fixture_name)
        # fail fast if we don't have valid json
        orjson.loads(body)
        return body


class MigrationsTestCase(ZulipTestCase):  # nocoverage
    """
    Test class for database migrations inspired by this blog post:
       https://www.caktusgroup.com/blog/2016/02/02/writing-unit-tests-django-migrations/
    Documented at https://zulip.readthedocs.io/en/latest/subsystems/schema-migrations.html
    """

    @property
    def app(self) -> str:
        app_config = apps.get_containing_app_config(type(self).__module__)
        assert app_config is not None
        return app_config.name

    migrate_from: Optional[str] = None
    migrate_to: Optional[str] = None

    def setUp(self) -> None:
        assert (
            self.migrate_from and self.migrate_to
        ), f"TestCase '{type(self).__name__}' must define migrate_from and migrate_to properties"
        migrate_from: List[Tuple[str, str]] = [(self.app, self.migrate_from)]
        migrate_to: List[Tuple[str, str]] = [(self.app, self.migrate_to)]
        executor = MigrationExecutor(connection)
        old_apps = executor.loader.project_state(migrate_from).apps

        # Reverse to the original migration
        executor.migrate(migrate_from)

        self.setUpBeforeMigration(old_apps)

        # Run the migration to test
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()  # reload.
        executor.migrate(migrate_to)

        self.apps = executor.loader.project_state(migrate_to).apps

    def setUpBeforeMigration(self, apps: StateApps) -> None:
        pass  # nocoverage
