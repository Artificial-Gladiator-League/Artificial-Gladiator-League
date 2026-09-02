from django.contrib.auth.tokens import PasswordResetTokenGenerator


class AccountActivationTokenGenerator(PasswordResetTokenGenerator):
    """HMAC token that includes ``is_active`` in the hash.

    Once the user is activated the hash input changes,
    making the old token cryptographically invalid — single-use by design.
    """

    def _make_hash_value(self, user, timestamp):
        return f"{user.pk}{timestamp}{user.is_active}"


account_activation_token = AccountActivationTokenGenerator()


class EmailChangeTokenGenerator(PasswordResetTokenGenerator):
    """HMAC token tied to pending_email; invalidates once that field changes or is cleared."""

    def _make_hash_value(self, user, timestamp):
        return f"{user.pk}{timestamp}{user.pending_email}"


email_change_token = EmailChangeTokenGenerator()
