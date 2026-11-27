"""Pure owner/readback contract; no Kodi SDK, locators in use, or network."""
import unittest

from player_ownership_model import (
    MediaFingerprint, PlayerOwnership, PlayerReadback, PlayerSession, PlayerState,
    ReadbackKind,
)


MEDIA = MediaFingerprint(b'a' * 32)
OTHER_MEDIA = MediaFingerprint(b'b' * 32)


class PlayerOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.owner = PlayerOwnership()
        self.session = PlayerSession()

    def send(self, *, with_session=True):
        self.session = PlayerSession()
        attempt = self.owner.begin()
        self.assertTrue(self.owner.mark_sent(attempt, MEDIA, self.session if with_session else None))
        return attempt

    def read(self, attempt, kind, *, media=None, session=None, terminal=None, control=False):
        ticket = self.owner.begin_read(attempt, for_control=control)
        self.assertIsNotNone(ticket)
        return self.owner.apply_readback(PlayerReadback(ticket, kind, media, session, terminal))

    def confirm(self, attempt):
        # Fingerprints compare by digest, not object identity; session does the opposite.
        self.assertFalse(self.read(attempt, ReadbackKind.ACTIVE,
                                   media=MediaFingerprint(b'a' * 32), session=self.session))
        self.assertIs(self.owner.state, PlayerState.ACTIVE)

    def test_single_pending_and_single_send_are_not_active(self):
        self.assertIs(self.owner.state, PlayerState.IDLE)
        old = self.owner.begin()
        current = self.owner.begin()
        self.assertIs(self.owner.state, PlayerState.PENDING)
        self.assertIsNone(self.owner.begin_read(current))
        self.assertFalse(self.owner.mark_sent(old, MEDIA, self.session))
        self.assertFalse(self.owner.cancel(old))
        self.assertFalse(self.owner.mark_sent(current, 'locator', self.session))
        self.assertFalse(self.owner.mark_sent(current, MEDIA, 1))  # reusable player slot
        self.assertTrue(self.owner.mark_sent(current, MEDIA, self.session))
        self.assertFalse(self.owner.mark_sent(current, OTHER_MEDIA, PlayerSession()))
        self.assertIs(self.owner.state, PlayerState.SENT)
        self.assertIsNone(self.owner.begin_read(current, for_control=True))
        self.confirm(current)

    def test_control_needs_fresh_scoped_read_and_cannot_be_replayed(self):
        attempt = self.send()
        self.confirm(attempt)  # a normal exact poll returns no control grant
        ticket = self.owner.begin_read(attempt, for_control=True)
        self.assertIsNotNone(ticket)
        sample = PlayerReadback(ticket, ReadbackKind.ACTIVE, MEDIA, self.session)
        self.assertTrue(self.owner.apply_readback(sample))
        self.assertFalse(self.owner.apply_readback(sample))
        self.assertIs(self.owner.state, PlayerState.ACTIVE)

    def test_exact_media_without_independent_session_never_confirms(self):
        attempt = self.send(with_session=False)
        for session in (None, PlayerSession()):
            self.assertFalse(self.read(attempt, ReadbackKind.ACTIVE, media=MEDIA, session=session))
            self.assertIs(self.owner.state, PlayerState.UNCONFIRMED)
            self.assertIsNone(self.owner.begin_read(attempt, for_control=True))

    def test_unrelated_during_async_start_can_be_followed_by_an_exact_start(self):
        attempt = self.send()
        self.assertFalse(self.read(attempt, ReadbackKind.ACTIVE, media=OTHER_MEDIA))
        self.assertIs(self.owner.state, PlayerState.UNRELATED)
        self.assertFalse(self.read(attempt, ReadbackKind.ACTIVE, media=MEDIA, session=PlayerSession()))
        self.assertIs(self.owner.state, PlayerState.UNRELATED)
        self.confirm(attempt)

    def test_replacement_revokes_controls_even_if_only_session_or_media_changes(self):
        for same_media in (True, False):
            with self.subTest(same_media=same_media):
                attempt = self.send()
                self.confirm(attempt)
                media = MEDIA if same_media else OTHER_MEDIA
                session = PlayerSession() if same_media else self.session
                self.assertFalse(self.read(attempt, ReadbackKind.ACTIVE, media=media,
                                           session=session, control=True))
                self.assertIs(self.owner.state, PlayerState.REPLACED)
                self.assertIsNone(self.owner.begin_read(attempt))  # cannot regain this admission

    def test_unknown_or_incomplete_read_withholds_controls_without_inventing_idle(self):
        attempt = self.send()
        for kind, media, session in ((ReadbackKind.UNKNOWN, None, None),
                                     (ReadbackKind.ACTIVE, MEDIA, None),
                                     (ReadbackKind.ACTIVE, None, self.session)):
            with self.subTest(kind=kind, media=media, session=session):
                self.confirm(attempt)
                self.assertFalse(self.read(attempt, kind, media=media, session=session, control=True))
                self.assertIs(self.owner.state, PlayerState.UNCONFIRMED)
                self.assertIsNone(self.owner.begin_read(attempt, for_control=True))
                self.assertFalse(self.owner.expire_startup(attempt))
        self.read(attempt, ReadbackKind.ACTIVE, media=OTHER_MEDIA)
        self.assertIs(self.owner.state, PlayerState.REPLACED)  # was confirmed before the gap

    def test_new_read_ticket_fences_an_out_of_order_terminal_read(self):
        attempt = self.send()
        self.confirm(attempt)
        older = self.owner.begin_read(attempt)
        latest = self.owner.begin_read(attempt, for_control=True)
        self.assertIsNotNone(older)
        self.assertIsNotNone(latest)
        self.assertIsNot(older, latest)
        stopped = PlayerReadback(older, ReadbackKind.IDLE, session=self.session,
                                 terminal=PlayerState.STOPPED)
        self.assertFalse(self.owner.apply_readback(stopped))
        self.assertIs(self.owner.state, PlayerState.ACTIVE)
        self.assertTrue(self.owner.apply_readback(PlayerReadback(latest, ReadbackKind.ACTIVE,
                                                                MEDIA, self.session)))
        self.assertFalse(self.owner.apply_readback(stopped))
        self.assertIs(self.owner.state, PlayerState.ACTIVE)

    def test_old_reads_and_commands_cannot_touch_new_start_of_same_media(self):
        first = self.send()
        self.confirm(first)
        ticket = self.owner.begin_read(first, for_control=True)
        self.assertIsNotNone(ticket)
        first_session = self.session
        old_terminal = PlayerReadback(ticket, ReadbackKind.IDLE, session=first_session,
                                      terminal=PlayerState.ENDED)
        old_active = PlayerReadback(ticket, ReadbackKind.ACTIVE, MEDIA, first_session)
        second = self.send()
        self.assertFalse(self.owner.apply_readback(old_active))
        self.assertIs(self.owner.state, PlayerState.SENT)
        # Even a fresh idle read with terminal evidence for the *old* session
        # cannot establish a cause for the new asynchronous send.
        self.read(second, ReadbackKind.IDLE, session=first_session, terminal=PlayerState.ENDED)
        self.assertIs(self.owner.state, PlayerState.UNCONFIRMED)
        self.confirm(second)
        self.assertFalse(self.owner.apply_readback(old_terminal))
        self.assertIsNone(self.owner.begin_read(first, for_control=True))
        self.assertFalse(self.owner.cancel(first))
        self.assertIs(self.owner.state, PlayerState.ACTIVE)

    def test_terminal_outcomes_need_idle_and_attributable_reason(self):
        for reason in (PlayerState.STOPPED, PlayerState.ENDED, PlayerState.ERROR):
            with self.subTest(reason=reason):
                attempt = self.send()
                self.confirm(attempt)
                self.assertFalse(self.read(attempt, ReadbackKind.UNKNOWN,
                                           session=self.session, terminal=reason, control=True))
                self.assertIs(self.owner.state, PlayerState.UNCONFIRMED)
                self.confirm(attempt)
                ticket = self.owner.begin_read(attempt, for_control=True)
                self.assertIsNotNone(ticket)
                terminal = PlayerReadback(ticket, ReadbackKind.IDLE, session=self.session,
                                          terminal=reason)
                self.assertFalse(self.owner.apply_readback(terminal))
                self.assertIs(self.owner.state, reason)
                self.assertIsNone(self.owner.begin_read(attempt))
                self.assertFalse(self.owner.apply_readback(terminal))
                self.assertIs(self.owner.state, reason)

    def test_definitive_idle_without_attribution_retires_previously_active_attempt(self):
        for session, reason in ((None, None), (PlayerSession(), PlayerState.STOPPED)):
            attempt = self.send()
            self.confirm(attempt)
            self.assertFalse(self.read(attempt, ReadbackKind.IDLE,
                                       session=session, terminal=reason, control=True))
            self.assertIs(self.owner.state, PlayerState.UNCONFIRMED)
            self.assertIsNone(self.owner.begin_read(attempt))

    def test_startup_error_can_be_attributed_but_plain_idle_only_expires(self):
        attempt = self.send()
        self.read(attempt, ReadbackKind.IDLE, session=self.session, terminal=PlayerState.ERROR)
        self.assertIs(self.owner.state, PlayerState.ERROR)
        self.assertIsNone(self.owner.begin_read(attempt))

        attempt = self.send()
        self.read(attempt, ReadbackKind.IDLE)
        self.assertIs(self.owner.state, PlayerState.UNCONFIRMED)
        late = self.owner.begin_read(attempt)
        self.assertIsNotNone(late)
        self.assertTrue(self.owner.expire_startup(attempt))
        self.assertFalse(self.owner.apply_readback(PlayerReadback(late, ReadbackKind.ACTIVE,
                                                                 MEDIA, self.session)))
        self.assertIsNone(self.owner.begin_read(attempt))
        self.assertIs(self.owner.state, PlayerState.UNCONFIRMED)

    def test_cancel_detaches_pending_or_active_without_a_terminal_claim(self):
        pending = self.owner.begin()
        self.assertTrue(self.owner.cancel(pending))
        self.assertFalse(self.owner.mark_sent(pending, MEDIA, self.session))
        attempt = self.send()
        self.confirm(attempt)
        ticket = self.owner.begin_read(attempt, for_control=True)
        self.assertIsNotNone(ticket)
        self.assertTrue(self.owner.cancel(attempt))
        self.assertFalse(self.owner.apply_readback(PlayerReadback(ticket, ReadbackKind.ACTIVE,
                                                                 MEDIA, self.session)))
        self.assertIs(self.owner.state, PlayerState.IDLE)

    def test_fingerprint_and_sample_reprs_do_not_show_private_values(self):
        for invalid in ('locator', b'short', bytearray(32)):
            with self.assertRaisesRegex(ValueError, '^invalid media fingerprint$'):
                MediaFingerprint(invalid)
        self.assertNotIn('a' * 32, repr(MEDIA))
        ticket = self.owner.begin_read(self.send())
        self.assertIsNotNone(ticket)
        sample = PlayerReadback(ticket, ReadbackKind.ACTIVE, MEDIA, self.session)
        self.assertNotIn(repr(MEDIA), repr(sample))
        self.assertNotIn(repr(self.session), repr(sample))
        self.assertNotIn(repr(ticket), repr(sample))
