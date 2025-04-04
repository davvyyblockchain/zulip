import logging
from typing import Any, Dict, Optional, Union

import stripe
from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.timezone import now as timezone_now
from django.utils.translation import gettext as _

from corporate.lib.stripe import (
    STRIPE_PUBLISHABLE_KEY,
    cents_to_dollar_string,
    do_change_plan_status,
    do_replace_payment_source,
    downgrade_at_the_end_of_billing_cycle,
    downgrade_now_without_creating_additional_invoices,
    get_latest_seat_count,
    make_end_of_cycle_updates_if_needed,
    renewal_amount,
    start_of_next_billing_cycle,
    stripe_get_customer,
    update_license_ledger_for_manual_plan,
    validate_licenses,
)
from corporate.models import (
    CustomerPlan,
    get_current_plan_by_customer,
    get_current_plan_by_realm,
    get_customer_by_realm,
)
from zerver.decorator import require_billing_access, zulip_login_required
from zerver.lib.exceptions import JsonableError
from zerver.lib.request import REQ, has_request_variables
from zerver.lib.response import json_success
from zerver.lib.validator import check_int, check_int_in
from zerver.models import UserProfile

billing_logger = logging.getLogger("corporate.stripe")


# Should only be called if the customer is being charged automatically
def payment_method_string(stripe_customer: stripe.Customer) -> str:
    stripe_source: Optional[Union[stripe.Card, stripe.Source]] = stripe_customer.default_source
    # In case of e.g. an expired card
    if stripe_source is None:  # nocoverage
        return _("No payment method on file")
    if stripe_source.object == "card":
        assert isinstance(stripe_source, stripe.Card)
        return _("{brand} ending in {last4}").format(
            brand=stripe_source.brand,
            last4=stripe_source.last4,
        )
    # There might be one-off stuff we do for a particular customer that
    # would land them here. E.g. by default we don't support ACH for
    # automatic payments, but in theory we could add it for a customer via
    # the Stripe dashboard.
    return _("Unknown payment method. Please contact {email}.").format(
        email=settings.ZULIP_ADMINISTRATOR,
    )  # nocoverage


@zulip_login_required
def billing_home(request: HttpRequest) -> HttpResponse:
    user = request.user
    assert user.is_authenticated

    customer = get_customer_by_realm(user.realm)
    context: Dict[str, Any] = {
        "admin_access": user.has_billing_access,
        "has_active_plan": False,
    }

    if user.realm.plan_type == user.realm.STANDARD_FREE:
        context["is_sponsored"] = True
        return render(request, "corporate/billing.html", context=context)

    if customer is None:
        from corporate.views.upgrade import initial_upgrade

        return HttpResponseRedirect(reverse(initial_upgrade))

    if customer.sponsorship_pending:
        context["sponsorship_pending"] = True
        return render(request, "corporate/billing.html", context=context)

    if not CustomerPlan.objects.filter(customer=customer).exists():
        from corporate.views.upgrade import initial_upgrade

        return HttpResponseRedirect(reverse(initial_upgrade))

    if not user.has_billing_access:
        return render(request, "corporate/billing.html", context=context)

    plan = get_current_plan_by_customer(customer)
    if plan is not None:
        now = timezone_now()
        new_plan, last_ledger_entry = make_end_of_cycle_updates_if_needed(plan, now)
        if last_ledger_entry is not None:
            if new_plan is not None:  # nocoverage
                plan = new_plan
            assert plan is not None  # for mypy
            downgrade_at_end_of_cycle = plan.status == CustomerPlan.DOWNGRADE_AT_END_OF_CYCLE
            switch_to_annual_at_end_of_cycle = (
                plan.status == CustomerPlan.SWITCH_TO_ANNUAL_AT_END_OF_CYCLE
            )
            licenses = last_ledger_entry.licenses
            licenses_at_next_renewal = last_ledger_entry.licenses_at_next_renewal
            seat_count = get_latest_seat_count(user.realm)

            # Should do this in javascript, using the user's timezone
            renewal_date = "{dt:%B} {dt.day}, {dt.year}".format(
                dt=start_of_next_billing_cycle(plan, now)
            )
            renewal_cents = renewal_amount(plan, now)
            charge_automatically = plan.charge_automatically
            assert customer.stripe_customer_id is not None  # for mypy
            stripe_customer = stripe_get_customer(customer.stripe_customer_id)
            if charge_automatically:
                payment_method = payment_method_string(stripe_customer)
            else:
                payment_method = "Billed by invoice"

            context.update(
                plan_name=plan.name,
                has_active_plan=True,
                free_trial=plan.is_free_trial(),
                downgrade_at_end_of_cycle=downgrade_at_end_of_cycle,
                automanage_licenses=plan.automanage_licenses,
                switch_to_annual_at_end_of_cycle=switch_to_annual_at_end_of_cycle,
                licenses=licenses,
                licenses_at_next_renewal=licenses_at_next_renewal,
                seat_count=seat_count,
                renewal_date=renewal_date,
                renewal_amount=cents_to_dollar_string(renewal_cents),
                payment_method=payment_method,
                charge_automatically=charge_automatically,
                publishable_key=STRIPE_PUBLISHABLE_KEY,
                stripe_email=stripe_customer.email,
                CustomerPlan=CustomerPlan,
                onboarding=request.GET.get("onboarding") is not None,
            )

    return render(request, "corporate/billing.html", context=context)


@require_billing_access
@has_request_variables
def update_plan(
    request: HttpRequest,
    user: UserProfile,
    status: Optional[int] = REQ(
        "status",
        json_validator=check_int_in(
            [
                CustomerPlan.ACTIVE,
                CustomerPlan.DOWNGRADE_AT_END_OF_CYCLE,
                CustomerPlan.SWITCH_TO_ANNUAL_AT_END_OF_CYCLE,
                CustomerPlan.ENDED,
            ]
        ),
        default=None,
    ),
    licenses: Optional[int] = REQ("licenses", json_validator=check_int, default=None),
    licenses_at_next_renewal: Optional[int] = REQ(
        "licenses_at_next_renewal", json_validator=check_int, default=None
    ),
) -> HttpResponse:
    plan = get_current_plan_by_realm(user.realm)
    assert plan is not None  # for mypy

    new_plan, last_ledger_entry = make_end_of_cycle_updates_if_needed(plan, timezone_now())
    if new_plan is not None:
        raise JsonableError(
            _("Unable to update the plan. The plan has been expired and replaced with a new plan.")
        )

    if last_ledger_entry is None:
        raise JsonableError(_("Unable to update the plan. The plan has ended."))

    if status is not None:
        if status == CustomerPlan.ACTIVE:
            assert plan.status == CustomerPlan.DOWNGRADE_AT_END_OF_CYCLE
            do_change_plan_status(plan, status)
        elif status == CustomerPlan.DOWNGRADE_AT_END_OF_CYCLE:
            assert plan.status == CustomerPlan.ACTIVE
            downgrade_at_the_end_of_billing_cycle(user.realm)
        elif status == CustomerPlan.SWITCH_TO_ANNUAL_AT_END_OF_CYCLE:
            assert plan.billing_schedule == CustomerPlan.MONTHLY
            assert plan.status == CustomerPlan.ACTIVE
            assert plan.fixed_price is None
            do_change_plan_status(plan, status)
        elif status == CustomerPlan.ENDED:
            assert plan.is_free_trial()
            downgrade_now_without_creating_additional_invoices(user.realm)
        return json_success()

    if licenses is not None:
        if plan.automanage_licenses:
            raise JsonableError(
                _(
                    "Unable to update licenses manually. Your plan is on automatic license management."
                )
            )
        if last_ledger_entry.licenses == licenses:
            raise JsonableError(
                _(
                    "Your plan is already on {licenses} licenses in the current billing period."
                ).format(licenses=licenses)
            )
        if last_ledger_entry.licenses > licenses:
            raise JsonableError(
                _("You cannot decrease the licenses in the current billing period.").format(
                    licenses=licenses
                )
            )
        validate_licenses(plan.charge_automatically, licenses, get_latest_seat_count(user.realm))
        update_license_ledger_for_manual_plan(plan, timezone_now(), licenses=licenses)
        return json_success()

    if licenses_at_next_renewal is not None:
        if plan.automanage_licenses:
            raise JsonableError(
                _(
                    "Unable to update licenses manually. Your plan is on automatic license management."
                )
            )
        if last_ledger_entry.licenses_at_next_renewal == licenses_at_next_renewal:
            raise JsonableError(
                _(
                    "Your plan is already scheduled to renew with {licenses_at_next_renewal} licenses."
                ).format(licenses_at_next_renewal=licenses_at_next_renewal)
            )
        validate_licenses(
            plan.charge_automatically,
            licenses_at_next_renewal,
            get_latest_seat_count(user.realm),
        )
        update_license_ledger_for_manual_plan(
            plan, timezone_now(), licenses_at_next_renewal=licenses_at_next_renewal
        )
        return json_success()

    raise JsonableError(_("Nothing to change."))


@require_billing_access
@has_request_variables
def replace_payment_source(
    request: HttpRequest,
    user: UserProfile,
    stripe_token: str = REQ(),
) -> HttpResponse:
    do_replace_payment_source(user, stripe_token, pay_invoices=True)
    return json_success()
