"""Plain callback mailbox bounds, ordering and owner affinity."""

from threading import Thread
import unittest

from channels_dvr.ui_dispatch import UiDispatcher


class UiDispatcherTests(unittest.TestCase):
    def test_fifo_bounds_and_drain_budget(self):
        queue = UiDispatcher()
        for index in range(128):
            self.assertTrue(queue.post(("action", index)))
        self.assertFalse(queue.post(("click", 100)))
        self.assertEqual(queue.dropped, 1)
        self.assertEqual(queue.drain(), tuple(("action", i) for i in range(16)))
        self.assertTrue(queue.post(("initialize", None)))
        remaining = []
        for _ in range(8):
            remaining.extend(queue.drain())
        self.assertEqual(remaining[-1], ("initialize", None))
        self.assertEqual(queue.drain(), ())

    def test_background_posts_owner_drains_and_close_invalidates(self):
        queue = UiDispatcher()
        errors = []
        def worker():
            queue.post(("click", 100))
            for operation in (queue.drain, queue.close):
                try:
                    operation()
                except RuntimeError:
                    errors.append("owner")
        thread = Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(errors, ["owner", "owner"])
        self.assertEqual(queue.drain(), (("click", 100),))
        queue.post(("action", 4))
        queue.close()
        queue.close()
        self.assertFalse(queue.post(("click", 100)))
        self.assertEqual(queue.drain(), ())

    def test_plain_commands_and_budget_validation(self):
        queue = UiDispatcher()
        for command in (("other", 1), ("initialize", 1), ("click", object()),
                        ("action", True), ["click", 100]):
            with self.assertRaises(ValueError):
                queue.post(command)
        for budget in (0, 17, True):
            with self.assertRaises(ValueError):
                queue.drain(budget)

    def test_transition_skips_already_popped_and_not_yet_popped_input(self):
        queue = UiDispatcher()
        for _ in range(20):
            queue.post(('action', 92))
        current = queue.dispatch()
        self.assertEqual(next(current), ('action', 92))
        queue.fence_input()
        self.assertEqual(list(current), [])
        queue.post(('action', 3))
        self.assertEqual(list(queue.dispatch()), [('action', 3)])

    def test_modal_and_reinitialization_do_not_replay_parent_input(self):
        queue = UiDispatcher()
        queue.post(('action', 92))
        with queue.modal_input():
            self.assertFalse(queue.post(('action', 92)))
            self.assertEqual(list(queue.dispatch()), [])
        queue.post(('action', 92))
        queue.post(('initialize', None))
        self.assertEqual(list(queue.dispatch()), [('initialize', None)])

    def test_full_mailbox_does_not_shed_window_lifecycle(self):
        queue = UiDispatcher()
        for _ in range(128):
            self.assertTrue(queue.post(('action', 92)))
        self.assertTrue(queue.post(('initialize', None)))
        self.assertEqual(list(queue.dispatch()), [('initialize', None)])
