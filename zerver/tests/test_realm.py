import datetime
import re
from typing import Any, Dict, List, Mapping, Union
from unittest import mock

import orjson
from django.conf import settings
from django.utils.timezone import now as timezone_now

from confirmation.models import Confirmation, create_confirmation_link
from zerver.lib.actions import (
    do_add_deactivated_redirect,
    do_change_plan_type,
    do_change_realm_subdomain,
    do_create_realm,
    do_deactivate_realm,
    do_deactivate_stream,
    do_scrub_realm,
    do_send_realm_reactivation_email,
    do_set_realm_property,
)
from zerver.lib.realm_description import get_realm_rendered_description, get_realm_text_description
from zerver.lib.send_email import send_future_email
from zerver.lib.streams import create_stream_if_needed
from zerver.lib.test_classes import ZulipTestCase
from zerver.models import (
    Attachment,
    CustomProfileField,
    Message,
    Realm,
    RealmAuditLog,
    ScheduledEmail,
    Stream,
    UserMessage,
    UserProfile,
    get_realm,
    get_stream,
    get_user_profile_by_id,
)


class RealmTest(ZulipTestCase):
    def assert_user_profile_cache_gets_new_name(
        self, user_profile: UserProfile, new_realm_name: str
    ) -> None:
        self.assertEqual(user_profile.realm.name, new_realm_name)

    def test_realm_creation_ensures_internal_realms(self) -> None:
        with mock.patch("zerver.lib.actions.server_initialized", return_value=False):
            with mock.patch(
                "zerver.lib.actions.create_internal_realm"
            ) as mock_create_internal, self.assertLogs(level="INFO") as info_logs:
                do_create_realm("testrealm", "Test Realm")
                mock_create_internal.assert_called_once()
            self.assertEqual(
                info_logs.output,
                ["INFO:root:Server not yet initialized. Creating the internal realm first."],
            )

    def test_do_set_realm_name_caching(self) -> None:
        """The main complicated thing about setting realm names is fighting the
        cache, and we start by populating the cache for Hamlet, and we end
        by checking the cache to ensure that the new value is there."""
        realm = get_realm("zulip")
        new_name = "Zed You Elle Eye Pea"
        do_set_realm_property(realm, "name", new_name, acting_user=None)
        self.assertEqual(get_realm(realm.string_id).name, new_name)
        self.assert_user_profile_cache_gets_new_name(self.example_user("hamlet"), new_name)

    def test_update_realm_name_events(self) -> None:
        realm = get_realm("zulip")
        new_name = "Puliz"
        events: List[Mapping[str, Any]] = []
        with self.tornado_redirected_to_list(events, expected_num_events=1):
            do_set_realm_property(realm, "name", new_name, acting_user=None)
        event = events[0]["event"]
        self.assertEqual(
            event,
            dict(
                type="realm",
                op="update",
                property="name",
                value=new_name,
            ),
        )

    def test_update_realm_description_events(self) -> None:
        realm = get_realm("zulip")
        new_description = "zulip dev group"
        events: List[Mapping[str, Any]] = []
        with self.tornado_redirected_to_list(events, expected_num_events=1):
            do_set_realm_property(realm, "description", new_description, acting_user=None)
        event = events[0]["event"]
        self.assertEqual(
            event,
            dict(
                type="realm",
                op="update",
                property="description",
                value=new_description,
            ),
        )

    def test_update_realm_description(self) -> None:
        self.login("iago")
        new_description = "zulip dev group"
        data = dict(description=new_description)
        events: List[Mapping[str, Any]] = []
        with self.tornado_redirected_to_list(events, expected_num_events=1):
            result = self.client_patch("/json/realm", data)
            self.assert_json_success(result)
            realm = get_realm("zulip")
            self.assertEqual(realm.description, new_description)

        event = events[0]["event"]
        self.assertEqual(
            event,
            dict(
                type="realm",
                op="update",
                property="description",
                value=new_description,
            ),
        )

    def test_realm_description_length(self) -> None:
        new_description = "A" * 1001
        data = dict(description=new_description)

        # create an admin user
        self.login("iago")

        result = self.client_patch("/json/realm", data)
        self.assert_json_error(result, "description is too long (limit: 1000 characters)")
        realm = get_realm("zulip")
        self.assertNotEqual(realm.description, new_description)

    def test_realm_name_length(self) -> None:
        new_name = "A" * (Realm.MAX_REALM_NAME_LENGTH + 1)
        data = dict(name=new_name)

        # create an admin user
        self.login("iago")

        result = self.client_patch("/json/realm", data)
        self.assert_json_error(result, "name is too long (limit: 40 characters)")
        realm = get_realm("zulip")
        self.assertNotEqual(realm.name, new_name)

    def test_admin_restrictions_for_changing_realm_name(self) -> None:
        new_name = "Mice will play while the cat is away"

        self.login("othello")

        req = dict(name=new_name)
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Must be an organization administrator")

    def test_unauthorized_name_change(self) -> None:
        data = {"full_name": "Sir Hamlet"}
        user_profile = self.example_user("hamlet")
        self.login_user(user_profile)
        do_set_realm_property(user_profile.realm, "name_changes_disabled", True, acting_user=None)
        url = "/json/settings"
        result = self.client_patch(url, data)
        self.assertEqual(result.status_code, 200)
        # Since the setting fails silently, no message is returned
        self.assert_in_response("", result)
        # Realm admins can change their name even setting is disabled.
        data = {"full_name": "New Iago"}
        self.login("iago")
        url = "/json/settings"
        result = self.client_patch(url, data)
        self.assert_json_success(result)

    def test_do_deactivate_realm_clears_user_realm_cache(self) -> None:
        """The main complicated thing about deactivating realm names is
        updating the cache, and we start by populating the cache for
        Hamlet, and we end by checking the cache to ensure that his
        realm appears to be deactivated.  You can make this test fail
        by disabling cache.flush_realm()."""
        hamlet_id = self.example_user("hamlet").id
        get_user_profile_by_id(hamlet_id)
        realm = get_realm("zulip")
        do_deactivate_realm(realm, acting_user=None)
        user = get_user_profile_by_id(hamlet_id)
        self.assertTrue(user.realm.deactivated)

    def test_do_change_realm_subdomain_clears_user_realm_cache(self) -> None:
        """The main complicated thing about changing realm subdomains is
        updating the cache, and we start by populating the cache for
        Hamlet, and we end by checking the cache to ensure that his
        realm appears to be deactivated.  You can make this test fail
        by disabling cache.flush_realm()."""
        hamlet_id = self.example_user("hamlet").id
        user = get_user_profile_by_id(hamlet_id)
        realm = get_realm("zulip")
        iago = self.example_user("iago")
        do_change_realm_subdomain(realm, "newzulip", acting_user=iago)
        user = get_user_profile_by_id(hamlet_id)
        self.assertEqual(user.realm.string_id, "newzulip")

        placeholder_realm = get_realm("zulip")
        self.assertTrue(placeholder_realm.deactivated)
        self.assertEqual(placeholder_realm.deactivated_redirect, user.realm.uri)

        realm_audit_log = RealmAuditLog.objects.filter(
            event_type=RealmAuditLog.REALM_SUBDOMAIN_CHANGED, acting_user=iago
        ).last()
        assert realm_audit_log is not None
        expected_extra_data = {"old_subdomain": "zulip", "new_subdomain": "newzulip"}
        self.assertEqual(realm_audit_log.extra_data, str(expected_extra_data))
        self.assertEqual(realm_audit_log.acting_user, iago)

    def test_do_deactivate_realm_clears_scheduled_jobs(self) -> None:
        user = self.example_user("hamlet")
        send_future_email(
            "zerver/emails/followup_day1",
            user.realm,
            to_user_ids=[user.id],
            delay=datetime.timedelta(hours=1),
        )
        self.assertEqual(ScheduledEmail.objects.count(), 1)
        do_deactivate_realm(user.realm, acting_user=None)
        self.assertEqual(ScheduledEmail.objects.count(), 0)

    def test_do_change_realm_description_clears_cached_descriptions(self) -> None:
        realm = get_realm("zulip")
        rendered_description = get_realm_rendered_description(realm)
        text_description = get_realm_text_description(realm)

        realm.description = "New description"
        realm.save(update_fields=["description"])

        new_rendered_description = get_realm_rendered_description(realm)
        self.assertNotEqual(rendered_description, new_rendered_description)
        self.assertIn(realm.description, new_rendered_description)

        new_text_description = get_realm_text_description(realm)
        self.assertNotEqual(text_description, new_text_description)
        self.assertEqual(realm.description, new_text_description)

    def test_do_deactivate_realm_on_deactivated_realm(self) -> None:
        """Ensure early exit is working in realm deactivation"""
        realm = get_realm("zulip")
        self.assertFalse(realm.deactivated)

        do_deactivate_realm(realm, acting_user=None)
        self.assertTrue(realm.deactivated)

        do_deactivate_realm(realm, acting_user=None)
        self.assertTrue(realm.deactivated)

    def test_do_set_deactivated_redirect_on_deactivated_realm(self) -> None:
        """Ensure that the redirect url is working when deactivating realm"""
        realm = get_realm("zulip")

        redirect_url = "new_server.zulip.com"
        do_deactivate_realm(realm, acting_user=None)
        self.assertTrue(realm.deactivated)
        do_add_deactivated_redirect(realm, redirect_url)
        self.assertEqual(realm.deactivated_redirect, redirect_url)

        new_redirect_url = "test.zulip.com"
        do_add_deactivated_redirect(realm, new_redirect_url)
        self.assertEqual(realm.deactivated_redirect, new_redirect_url)
        self.assertNotEqual(realm.deactivated_redirect, redirect_url)

    def test_realm_reactivation_link(self) -> None:
        realm = get_realm("zulip")
        do_deactivate_realm(realm, acting_user=None)
        self.assertTrue(realm.deactivated)
        confirmation_url = create_confirmation_link(realm, Confirmation.REALM_REACTIVATION)
        response = self.client_get(confirmation_url)
        self.assert_in_success_response(
            ["Your organization has been successfully reactivated"], response
        )
        realm = get_realm("zulip")
        self.assertFalse(realm.deactivated)

    def test_realm_reactivation_confirmation_object(self) -> None:
        realm = get_realm("zulip")
        do_deactivate_realm(realm, acting_user=None)
        self.assertTrue(realm.deactivated)
        create_confirmation_link(realm, Confirmation.REALM_REACTIVATION)
        confirmation = Confirmation.objects.last()
        assert confirmation is not None
        self.assertEqual(confirmation.content_object, realm)
        self.assertEqual(confirmation.realm, realm)

    def test_do_send_realm_reactivation_email(self) -> None:
        realm = get_realm("zulip")
        iago = self.example_user("iago")
        do_send_realm_reactivation_email(realm, acting_user=iago)
        from django.core.mail import outbox

        self.assert_length(outbox, 1)
        self.assertEqual(self.email_envelope_from(outbox[0]), settings.NOREPLY_EMAIL_ADDRESS)
        self.assertRegex(
            self.email_display_from(outbox[0]),
            fr"^Zulip Account Security <{self.TOKENIZED_NOREPLY_REGEX}>\Z",
        )
        self.assertIn("Reactivate your Zulip organization", outbox[0].subject)
        self.assertIn("Dear former administrators", outbox[0].body)
        admins = realm.get_human_admin_users()
        confirmation_url = self.get_confirmation_url_from_outbox(admins[0].delivery_email)
        response = self.client_get(confirmation_url)
        self.assert_in_success_response(
            ["Your organization has been successfully reactivated"], response
        )
        realm = get_realm("zulip")
        self.assertFalse(realm.deactivated)
        self.assertEqual(
            RealmAuditLog.objects.filter(
                event_type=RealmAuditLog.REALM_REACTIVATION_EMAIL_SENT, acting_user=iago
            ).count(),
            1,
        )

    def test_realm_reactivation_with_random_link(self) -> None:
        random_link = "/reactivate/5e89081eb13984e0f3b130bf7a4121d153f1614b"
        response = self.client_get(random_link)
        self.assert_in_success_response(
            ["The organization reactivation link has expired or is not valid."], response
        )

    def test_change_notifications_stream(self) -> None:
        # We need an admin user.
        self.login("iago")

        disabled_notif_stream_id = -1
        req = dict(notifications_stream_id=orjson.dumps(disabled_notif_stream_id).decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        realm = get_realm("zulip")
        self.assertEqual(realm.notifications_stream, None)

        new_notif_stream_id = Stream.objects.get(name="Denmark").id
        req = dict(notifications_stream_id=orjson.dumps(new_notif_stream_id).decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        realm = get_realm("zulip")
        assert realm.notifications_stream is not None
        self.assertEqual(realm.notifications_stream.id, new_notif_stream_id)

        invalid_notif_stream_id = 1234
        req = dict(notifications_stream_id=orjson.dumps(invalid_notif_stream_id).decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Invalid stream id")
        realm = get_realm("zulip")
        assert realm.notifications_stream is not None
        self.assertNotEqual(realm.notifications_stream.id, invalid_notif_stream_id)

    def test_get_default_notifications_stream(self) -> None:
        realm = get_realm("zulip")
        verona = get_stream("verona", realm)

        notifications_stream = realm.get_notifications_stream()
        assert notifications_stream is not None
        self.assertEqual(notifications_stream.id, verona.id)
        do_deactivate_stream(notifications_stream, acting_user=None)
        self.assertIsNone(realm.get_notifications_stream())

    def test_change_signup_notifications_stream(self) -> None:
        # We need an admin user.
        self.login("iago")

        disabled_signup_notifications_stream_id = -1
        req = dict(
            signup_notifications_stream_id=orjson.dumps(
                disabled_signup_notifications_stream_id
            ).decode()
        )
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        realm = get_realm("zulip")
        self.assertEqual(realm.signup_notifications_stream, None)

        new_signup_notifications_stream_id = Stream.objects.get(name="Denmark").id
        req = dict(
            signup_notifications_stream_id=orjson.dumps(new_signup_notifications_stream_id).decode()
        )

        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        realm = get_realm("zulip")
        assert realm.signup_notifications_stream is not None
        self.assertEqual(realm.signup_notifications_stream.id, new_signup_notifications_stream_id)

        invalid_signup_notifications_stream_id = 1234
        req = dict(
            signup_notifications_stream_id=orjson.dumps(
                invalid_signup_notifications_stream_id
            ).decode()
        )
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Invalid stream id")
        realm = get_realm("zulip")
        assert realm.signup_notifications_stream is not None
        self.assertNotEqual(
            realm.signup_notifications_stream.id, invalid_signup_notifications_stream_id
        )

    def test_get_default_signup_notifications_stream(self) -> None:
        realm = get_realm("zulip")
        verona = get_stream("verona", realm)
        realm.signup_notifications_stream = verona
        realm.save(update_fields=["signup_notifications_stream"])

        signup_notifications_stream = realm.get_signup_notifications_stream()
        assert signup_notifications_stream is not None
        self.assertEqual(signup_notifications_stream, verona)
        do_deactivate_stream(signup_notifications_stream, acting_user=None)
        self.assertIsNone(realm.get_signup_notifications_stream())

    def test_change_realm_default_language(self) -> None:
        # we need an admin user.
        self.login("iago")
        # Test to make sure that when invalid languages are passed
        # as the default realm language, correct validation error is
        # raised and the invalid language is not saved in db
        invalid_lang = "invalid_lang"
        req = dict(default_language=invalid_lang)
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, f"Invalid language '{invalid_lang}'")
        realm = get_realm("zulip")
        self.assertNotEqual(realm.default_language, invalid_lang)

    def test_deactivate_realm_by_owner(self) -> None:
        self.login("desdemona")
        realm = get_realm("zulip")
        self.assertFalse(realm.deactivated)

        result = self.client_post("/json/realm/deactivate")
        self.assert_json_success(result)
        realm = get_realm("zulip")
        self.assertTrue(realm.deactivated)

    def test_deactivate_realm_by_non_owner(self) -> None:
        self.login("iago")
        realm = get_realm("zulip")
        self.assertFalse(realm.deactivated)

        result = self.client_post("/json/realm/deactivate")
        self.assert_json_error(result, "Must be an organization owner")
        realm = get_realm("zulip")
        self.assertFalse(realm.deactivated)

    def test_invalid_integer_attribute_values(self) -> None:

        integer_values = [key for key, value in Realm.property_types.items() if value is int]

        invalid_values = dict(
            bot_creation_policy=10,
            create_stream_policy=10,
            invite_to_stream_policy=10,
            email_address_visibility=10,
            message_retention_days=10,
            video_chat_provider=10,
            giphy_rating=10,
            waiting_period_threshold=-10,
            digest_weekday=10,
            user_group_edit_policy=10,
            private_message_policy=10,
            message_content_delete_limit_seconds=-10,
            wildcard_mention_policy=10,
            invite_to_realm_policy=10,
            move_messages_between_streams_policy=10,
            add_custom_emoji_policy=10,
        )

        # We need an admin user.
        self.login("iago")

        for name in integer_values:
            invalid_value = invalid_values.get(name)
            if invalid_value is None:
                raise AssertionError(f"No test created for {name}")

            self.do_test_invalid_integer_attribute_value(name, invalid_value)

    def do_test_invalid_integer_attribute_value(self, val_name: str, invalid_val: int) -> None:

        possible_messages = {
            f"Invalid {val_name}",
            f"Bad value for '{val_name}'",
            f"Bad value for '{val_name}': {invalid_val}",
            f"Invalid {val_name} {invalid_val}",
        }

        req = {val_name: invalid_val}
        result = self.client_patch("/json/realm", req)
        msg = self.get_json_error(result)
        self.assertTrue(msg in possible_messages)

    def test_change_video_chat_provider(self) -> None:
        self.assertEqual(
            get_realm("zulip").video_chat_provider, Realm.VIDEO_CHAT_PROVIDERS["jitsi_meet"]["id"]
        )
        self.login("iago")

        invalid_video_chat_provider_value = 10
        req = {"video_chat_provider": orjson.dumps(invalid_video_chat_provider_value).decode()}
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(
            result, ("Invalid video_chat_provider {}").format(invalid_video_chat_provider_value)
        )

        req = {
            "video_chat_provider": orjson.dumps(
                Realm.VIDEO_CHAT_PROVIDERS["disabled"]["id"]
            ).decode()
        }
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        self.assertEqual(
            get_realm("zulip").video_chat_provider, Realm.VIDEO_CHAT_PROVIDERS["disabled"]["id"]
        )

        req = {
            "video_chat_provider": orjson.dumps(
                Realm.VIDEO_CHAT_PROVIDERS["jitsi_meet"]["id"]
            ).decode()
        }
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        self.assertEqual(
            get_realm("zulip").video_chat_provider, Realm.VIDEO_CHAT_PROVIDERS["jitsi_meet"]["id"]
        )

        req = {
            "video_chat_provider": orjson.dumps(
                Realm.VIDEO_CHAT_PROVIDERS["big_blue_button"]["id"]
            ).decode()
        }
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        self.assertEqual(
            get_realm("zulip").video_chat_provider,
            Realm.VIDEO_CHAT_PROVIDERS["big_blue_button"]["id"],
        )

        req = {
            "video_chat_provider": orjson.dumps(Realm.VIDEO_CHAT_PROVIDERS["zoom"]["id"]).decode()
        }
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)

    def test_initial_plan_type(self) -> None:
        with self.settings(BILLING_ENABLED=True):
            self.assertEqual(do_create_realm("hosted", "hosted").plan_type, Realm.LIMITED)
            self.assertEqual(
                get_realm("hosted").max_invites, settings.INVITES_DEFAULT_REALM_DAILY_MAX
            )
            self.assertEqual(
                get_realm("hosted").message_visibility_limit, Realm.MESSAGE_VISIBILITY_LIMITED
            )
            self.assertEqual(get_realm("hosted").upload_quota_gb, Realm.UPLOAD_QUOTA_LIMITED)

        with self.settings(BILLING_ENABLED=False):
            self.assertEqual(do_create_realm("onpremise", "onpremise").plan_type, Realm.SELF_HOSTED)
            self.assertEqual(
                get_realm("onpremise").max_invites, settings.INVITES_DEFAULT_REALM_DAILY_MAX
            )
            self.assertEqual(get_realm("onpremise").message_visibility_limit, None)
            self.assertEqual(get_realm("onpremise").upload_quota_gb, None)

    def test_change_plan_type(self) -> None:
        realm = get_realm("zulip")
        iago = self.example_user("iago")
        self.assertEqual(realm.plan_type, Realm.SELF_HOSTED)
        self.assertEqual(realm.max_invites, settings.INVITES_DEFAULT_REALM_DAILY_MAX)
        self.assertEqual(realm.message_visibility_limit, None)
        self.assertEqual(realm.upload_quota_gb, None)

        do_change_plan_type(realm, Realm.STANDARD, acting_user=iago)
        realm = get_realm("zulip")
        realm_audit_log = RealmAuditLog.objects.filter(
            event_type=RealmAuditLog.REALM_PLAN_TYPE_CHANGED
        ).last()
        assert realm_audit_log is not None
        expected_extra_data = {"old_value": Realm.SELF_HOSTED, "new_value": Realm.STANDARD}
        self.assertEqual(realm_audit_log.extra_data, str(expected_extra_data))
        self.assertEqual(realm_audit_log.acting_user, iago)
        self.assertEqual(realm.plan_type, Realm.STANDARD)
        self.assertEqual(realm.max_invites, Realm.INVITES_STANDARD_REALM_DAILY_MAX)
        self.assertEqual(realm.message_visibility_limit, None)
        self.assertEqual(realm.upload_quota_gb, Realm.UPLOAD_QUOTA_STANDARD)

        do_change_plan_type(realm, Realm.LIMITED, acting_user=iago)
        realm = get_realm("zulip")
        self.assertEqual(realm.plan_type, Realm.LIMITED)
        self.assertEqual(realm.max_invites, settings.INVITES_DEFAULT_REALM_DAILY_MAX)
        self.assertEqual(realm.message_visibility_limit, Realm.MESSAGE_VISIBILITY_LIMITED)
        self.assertEqual(realm.upload_quota_gb, Realm.UPLOAD_QUOTA_LIMITED)

        do_change_plan_type(realm, Realm.STANDARD_FREE, acting_user=iago)
        realm = get_realm("zulip")
        self.assertEqual(realm.plan_type, Realm.STANDARD_FREE)
        self.assertEqual(realm.max_invites, Realm.INVITES_STANDARD_REALM_DAILY_MAX)
        self.assertEqual(realm.message_visibility_limit, None)
        self.assertEqual(realm.upload_quota_gb, Realm.UPLOAD_QUOTA_STANDARD)

        do_change_plan_type(realm, Realm.LIMITED, acting_user=iago)

        do_change_plan_type(realm, Realm.SELF_HOSTED, acting_user=iago)
        self.assertEqual(realm.plan_type, Realm.SELF_HOSTED)
        self.assertEqual(realm.max_invites, settings.INVITES_DEFAULT_REALM_DAILY_MAX)
        self.assertEqual(realm.message_visibility_limit, None)
        self.assertEqual(realm.upload_quota_gb, None)

    def test_message_retention_days(self) -> None:
        self.login("iago")
        realm = get_realm("zulip")
        self.assertEqual(realm.plan_type, Realm.SELF_HOSTED)

        req = dict(message_retention_days=orjson.dumps(10).decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Must be an organization owner")

        self.login("desdemona")

        req = dict(message_retention_days=orjson.dumps(0).decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Bad value for 'message_retention_days': 0")

        req = dict(message_retention_days=orjson.dumps(-10).decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Bad value for 'message_retention_days': -10")

        req = dict(message_retention_days=orjson.dumps("invalid").decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Bad value for 'message_retention_days': invalid")

        req = dict(message_retention_days=orjson.dumps(-1).decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Bad value for 'message_retention_days': -1")

        req = dict(message_retention_days=orjson.dumps("forever").decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)

        req = dict(message_retention_days=orjson.dumps(10).decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)

        do_change_plan_type(realm, Realm.LIMITED, acting_user=None)
        req = dict(message_retention_days=orjson.dumps(10).decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Available on Zulip Standard. Upgrade to access.")

        do_change_plan_type(realm, Realm.STANDARD, acting_user=None)
        req = dict(message_retention_days=orjson.dumps(10).decode())
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)

    def test_do_create_realm(self) -> None:
        realm = do_create_realm("realm_string_id", "realm name")

        self.assertEqual(realm.string_id, "realm_string_id")
        self.assertEqual(realm.name, "realm name")
        self.assertFalse(realm.emails_restricted_to_domains)
        self.assertEqual(realm.email_address_visibility, Realm.EMAIL_ADDRESS_VISIBILITY_EVERYONE)
        self.assertEqual(realm.description, "")
        self.assertTrue(realm.invite_required)
        self.assertEqual(realm.plan_type, Realm.LIMITED)
        self.assertEqual(realm.org_type, Realm.ORG_TYPES["unspecified"]["id"])
        self.assertEqual(type(realm.date_created), datetime.datetime)

        self.assertTrue(
            RealmAuditLog.objects.filter(
                realm=realm, event_type=RealmAuditLog.REALM_CREATED, event_time=realm.date_created
            ).exists()
        )

        assert realm.notifications_stream is not None
        self.assertEqual(realm.notifications_stream.name, "general")
        self.assertEqual(realm.notifications_stream.realm, realm)

        assert realm.signup_notifications_stream is not None
        self.assertEqual(realm.signup_notifications_stream.name, "core team")
        self.assertEqual(realm.signup_notifications_stream.realm, realm)

        self.assertEqual(realm.plan_type, Realm.LIMITED)

    def test_do_create_realm_with_keyword_arguments(self) -> None:
        date_created = timezone_now() - datetime.timedelta(days=100)
        realm = do_create_realm(
            "realm_string_id",
            "realm name",
            emails_restricted_to_domains=True,
            date_created=date_created,
            email_address_visibility=Realm.EMAIL_ADDRESS_VISIBILITY_MEMBERS,
            description="realm description",
            invite_required=False,
            plan_type=Realm.STANDARD_FREE,
            org_type=Realm.ORG_TYPES["community"]["id"],
        )
        self.assertEqual(realm.string_id, "realm_string_id")
        self.assertEqual(realm.name, "realm name")
        self.assertTrue(realm.emails_restricted_to_domains)
        self.assertEqual(realm.email_address_visibility, Realm.EMAIL_ADDRESS_VISIBILITY_MEMBERS)
        self.assertEqual(realm.description, "realm description")
        self.assertFalse(realm.invite_required)
        self.assertEqual(realm.plan_type, Realm.STANDARD_FREE)
        self.assertEqual(realm.org_type, Realm.ORG_TYPES["community"]["id"])
        self.assertEqual(realm.date_created, date_created)

        self.assertTrue(
            RealmAuditLog.objects.filter(
                realm=realm, event_type=RealmAuditLog.REALM_CREATED, event_time=realm.date_created
            ).exists()
        )

        assert realm.notifications_stream is not None
        self.assertEqual(realm.notifications_stream.name, "general")
        self.assertEqual(realm.notifications_stream.realm, realm)

        assert realm.signup_notifications_stream is not None
        self.assertEqual(realm.signup_notifications_stream.name, "core team")
        self.assertEqual(realm.signup_notifications_stream.realm, realm)


class RealmAPITest(ZulipTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.login("desdemona")

    def set_up_db(self, attr: str, value: Any) -> None:
        realm = get_realm("zulip")
        setattr(realm, attr, value)
        realm.save(update_fields=[attr])

    def update_with_api(self, name: str, value: Union[int, str]) -> Realm:
        if not isinstance(value, str):
            value = orjson.dumps(value).decode()
        result = self.client_patch("/json/realm", {name: value})
        self.assert_json_success(result)
        return get_realm("zulip")  # refresh data

    def update_with_api_multiple_value(self, data_dict: Dict[str, Any]) -> Realm:
        result = self.client_patch("/json/realm", data_dict)
        self.assert_json_success(result)
        return get_realm("zulip")

    def do_test_realm_update_api(self, name: str) -> None:
        """Test updating realm properties.

        If new realm properties have been added to the Realm model but the
        test_values dict below has not been updated, this will raise an
        assertion error.
        """

        bool_tests: List[bool] = [False, True]
        test_values: Dict[str, Any] = dict(
            default_language=["de", "en"],
            default_code_block_language=["javascript", ""],
            description=["Realm description", "New description"],
            digest_weekday=[0, 1, 2],
            message_retention_days=[10, 20],
            name=["Zulip", "New Name"],
            waiting_period_threshold=[10, 20],
            create_stream_policy=Realm.COMMON_POLICY_TYPES,
            user_group_edit_policy=Realm.COMMON_POLICY_TYPES,
            private_message_policy=Realm.PRIVATE_MESSAGE_POLICY_TYPES,
            invite_to_stream_policy=Realm.COMMON_POLICY_TYPES,
            wildcard_mention_policy=Realm.WILDCARD_MENTION_POLICY_TYPES,
            bot_creation_policy=Realm.BOT_CREATION_POLICY_TYPES,
            email_address_visibility=Realm.EMAIL_ADDRESS_VISIBILITY_TYPES,
            video_chat_provider=[
                dict(
                    video_chat_provider=orjson.dumps(
                        Realm.VIDEO_CHAT_PROVIDERS["jitsi_meet"]["id"]
                    ).decode(),
                ),
            ],
            giphy_rating=[
                Realm.GIPHY_RATING_OPTIONS["y"]["id"],
                Realm.GIPHY_RATING_OPTIONS["r"]["id"],
            ],
            message_content_delete_limit_seconds=[1000, 1100, 1200],
            invite_to_realm_policy=Realm.INVITE_TO_REALM_POLICY_TYPES,
            move_messages_between_streams_policy=Realm.COMMON_POLICY_TYPES,
            add_custom_emoji_policy=Realm.COMMON_POLICY_TYPES,
        )

        vals = test_values.get(name)
        if Realm.property_types[name] is bool:
            vals = bool_tests
        if vals is None:
            raise AssertionError(f"No test created for {name}")

        if name == "video_chat_provider":
            self.set_up_db(name, vals[0][name])
            realm = self.update_with_api_multiple_value(vals[0])
            self.assertEqual(getattr(realm, name), orjson.loads(vals[0][name]))
            return

        self.set_up_db(name, vals[0])

        for val in vals[1:]:
            realm = self.update_with_api(name, val)
            self.assertEqual(getattr(realm, name), val)

        realm = self.update_with_api(name, vals[0])
        self.assertEqual(getattr(realm, name), vals[0])

    def test_update_realm_properties(self) -> None:
        for prop in Realm.property_types:
            with self.subTest(property=prop):
                self.do_test_realm_update_api(prop)

    def test_update_realm_allow_message_editing(self) -> None:
        """Tests updating the realm property 'allow_message_editing'."""
        self.set_up_db("allow_message_editing", False)
        self.set_up_db("message_content_edit_limit_seconds", 0)
        self.set_up_db("edit_topic_policy", Realm.POLICY_ADMINS_ONLY)
        realm = self.update_with_api("allow_message_editing", True)
        realm = self.update_with_api("message_content_edit_limit_seconds", 100)
        realm = self.update_with_api("edit_topic_policy", Realm.POLICY_EVERYONE)
        self.assertEqual(realm.allow_message_editing, True)
        self.assertEqual(realm.message_content_edit_limit_seconds, 100)
        self.assertEqual(realm.edit_topic_policy, Realm.POLICY_EVERYONE)
        realm = self.update_with_api("allow_message_editing", False)
        self.assertEqual(realm.allow_message_editing, False)
        self.assertEqual(realm.message_content_edit_limit_seconds, 100)
        self.assertEqual(realm.edit_topic_policy, Realm.POLICY_EVERYONE)
        realm = self.update_with_api("message_content_edit_limit_seconds", 200)
        self.assertEqual(realm.allow_message_editing, False)
        self.assertEqual(realm.message_content_edit_limit_seconds, 200)
        self.assertEqual(realm.edit_topic_policy, Realm.POLICY_EVERYONE)
        realm = self.update_with_api("edit_topic_policy", Realm.POLICY_ADMINS_ONLY)
        self.assertEqual(realm.allow_message_editing, False)
        self.assertEqual(realm.message_content_edit_limit_seconds, 200)
        self.assertEqual(realm.edit_topic_policy, Realm.POLICY_ADMINS_ONLY)

        realm = self.update_with_api("edit_topic_policy", Realm.POLICY_MODERATORS_ONLY)
        self.assertEqual(realm.allow_message_editing, False)
        self.assertEqual(realm.message_content_edit_limit_seconds, 200)
        self.assertEqual(realm.edit_topic_policy, Realm.POLICY_MODERATORS_ONLY)

        realm = self.update_with_api("edit_topic_policy", Realm.POLICY_FULL_MEMBERS_ONLY)
        self.assertEqual(realm.allow_message_editing, False)
        self.assertEqual(realm.message_content_edit_limit_seconds, 200)
        self.assertEqual(realm.edit_topic_policy, Realm.POLICY_FULL_MEMBERS_ONLY)

        realm = self.update_with_api("edit_topic_policy", Realm.POLICY_MEMBERS_ONLY)
        self.assertEqual(realm.allow_message_editing, False)
        self.assertEqual(realm.message_content_edit_limit_seconds, 200)
        self.assertEqual(realm.edit_topic_policy, Realm.POLICY_MEMBERS_ONLY)

        # Test an invalid value for edit_topic_policy
        invalid_edit_topic_policy_value = 10
        req = {"edit_topic_policy": orjson.dumps(invalid_edit_topic_policy_value).decode()}
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Invalid edit_topic_policy")

    def test_update_realm_allow_message_deleting(self) -> None:
        """Tests updating the realm property 'allow_message_deleting'."""
        self.set_up_db("allow_message_deleting", True)
        self.set_up_db("message_content_delete_limit_seconds", 0)
        realm = self.update_with_api("allow_message_deleting", False)
        self.assertEqual(realm.allow_message_deleting, False)
        self.assertEqual(realm.message_content_delete_limit_seconds, 0)
        realm = self.update_with_api("allow_message_deleting", True)
        realm = self.update_with_api("message_content_delete_limit_seconds", 100)
        self.assertEqual(realm.allow_message_deleting, True)
        self.assertEqual(realm.message_content_delete_limit_seconds, 100)
        realm = self.update_with_api("message_content_delete_limit_seconds", 600)
        self.assertEqual(realm.allow_message_deleting, True)
        self.assertEqual(realm.message_content_delete_limit_seconds, 600)

    def test_change_invite_to_realm_policy_by_owners_only(self) -> None:
        self.login("iago")
        req = {"invite_to_realm_policy": Realm.POLICY_ADMINS_ONLY}
        result = self.client_patch("/json/realm", req)
        self.assert_json_error(result, "Must be an organization owner")

        self.login("desdemona")
        result = self.client_patch("/json/realm", req)
        self.assert_json_success(result)
        realm = get_realm("zulip")
        self.assertEqual(realm.invite_to_realm_policy, Realm.POLICY_ADMINS_ONLY)


class ScrubRealmTest(ZulipTestCase):
    def test_scrub_realm(self) -> None:
        zulip = get_realm("zulip")
        lear = get_realm("lear")

        iago = self.example_user("iago")
        othello = self.example_user("othello")

        cordelia = self.lear_user("cordelia")
        king = self.lear_user("king")

        create_stream_if_needed(lear, "Shakespeare")

        self.subscribe(cordelia, "Shakespeare")
        self.subscribe(king, "Shakespeare")

        Message.objects.all().delete()
        UserMessage.objects.all().delete()

        for i in range(5):
            self.send_stream_message(iago, "Scotland")
            self.send_stream_message(othello, "Scotland")
            self.send_stream_message(cordelia, "Shakespeare")
            self.send_stream_message(king, "Shakespeare")

        Attachment.objects.filter(realm=zulip).delete()
        Attachment.objects.create(realm=zulip, owner=iago, path_id="a/b/temp1.txt", size=512)
        Attachment.objects.create(realm=zulip, owner=othello, path_id="a/b/temp2.txt", size=512)

        Attachment.objects.filter(realm=lear).delete()
        Attachment.objects.create(realm=lear, owner=cordelia, path_id="c/d/temp1.txt", size=512)
        Attachment.objects.create(realm=lear, owner=king, path_id="c/d/temp2.txt", size=512)

        CustomProfileField.objects.create(realm=lear)

        self.assertEqual(Message.objects.filter(sender__in=[iago, othello]).count(), 10)
        self.assertEqual(Message.objects.filter(sender__in=[cordelia, king]).count(), 10)
        self.assertEqual(UserMessage.objects.filter(user_profile__in=[iago, othello]).count(), 20)
        self.assertEqual(UserMessage.objects.filter(user_profile__in=[cordelia, king]).count(), 20)

        self.assertNotEqual(CustomProfileField.objects.filter(realm=zulip).count(), 0)

        with self.assertLogs(level="WARNING"):
            do_scrub_realm(zulip, acting_user=None)

        self.assertEqual(Message.objects.filter(sender__in=[iago, othello]).count(), 0)
        self.assertEqual(Message.objects.filter(sender__in=[cordelia, king]).count(), 10)
        self.assertEqual(UserMessage.objects.filter(user_profile__in=[iago, othello]).count(), 0)
        self.assertEqual(UserMessage.objects.filter(user_profile__in=[cordelia, king]).count(), 20)

        self.assertEqual(Attachment.objects.filter(realm=zulip).count(), 0)
        self.assertEqual(Attachment.objects.filter(realm=lear).count(), 2)

        self.assertEqual(CustomProfileField.objects.filter(realm=zulip).count(), 0)
        self.assertNotEqual(CustomProfileField.objects.filter(realm=lear).count(), 0)

        zulip_users = UserProfile.objects.filter(realm=zulip)
        for user in zulip_users:
            self.assertTrue(re.search("Scrubbed [a-z0-9]{15}", user.full_name))
            self.assertTrue(re.search("scrubbed-[a-z0-9]{15}@" + zulip.host, user.email))
            self.assertTrue(re.search("scrubbed-[a-z0-9]{15}@" + zulip.host, user.delivery_email))

        lear_users = UserProfile.objects.filter(realm=lear)
        for user in lear_users:
            self.assertIsNone(re.search("Scrubbed [a-z0-9]{15}", user.full_name))
            self.assertIsNone(re.search("scrubbed-[a-z0-9]{15}@" + zulip.host, user.email))
            self.assertIsNone(re.search("scrubbed-[a-z0-9]{15}@" + zulip.host, user.delivery_email))
