from __future__ import annotations

import asyncio
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mock_interview.controller import InterviewController, StateTransitionError
from mock_interview.state import InterviewSessionData, InterviewState, PastExperienceArea, TransitionReason


class InterviewStateMachineTests(unittest.IsolatedAsyncioTestCase):
    def make_controller(
        self,
        timeout: float = 10.0,
        past_timeout: float = 10.0,
        max_past_timeouts: int = 3,
    ) -> InterviewController:
        return InterviewController(
            InterviewSessionData(),
            self_intro_timeout_seconds=timeout,
            past_exp_timeout_seconds=past_timeout,
            max_past_exp_timeouts=max_past_timeouts,
            logger=logging.getLogger("test"),
        )

    async def test_normal_self_introduction_transition(self) -> None:
        controller = self.make_controller()
        await controller.start_stage(InterviewState.SELF_INTRODUCTION)

        result = await controller.transition_to_past_experience(
            reason=TransitionReason.NORMAL_COMPLETION
        )

        self.assertTrue(result.transitioned)
        self.assertEqual(controller.userdata.current_stage, InterviewState.PAST_EXPERIENCE)
        self.assertTrue(controller.userdata.self_intro_completed)
        self.assertEqual(controller.userdata.transition_reason, TransitionReason.NORMAL_COMPLETION)
        self.assertFalse(controller.userdata.fallback_triggered)

    async def test_timeout_fallback_transition(self) -> None:
        controller = self.make_controller(timeout=0.01)
        await controller.start_stage(InterviewState.SELF_INTRODUCTION)
        handoff_called = asyncio.Event()

        async def callback(_result) -> None:
            handoff_called.set()

        await controller.schedule_self_intro_timeout(callback)
        await asyncio.wait_for(handoff_called.wait(), timeout=1.0)

        self.assertEqual(controller.userdata.current_stage, InterviewState.PAST_EXPERIENCE)
        self.assertEqual(controller.userdata.transition_reason, TransitionReason.TIMEOUT_FALLBACK)
        self.assertTrue(controller.userdata.fallback_triggered)

    async def test_self_intro_timeout_not_scheduled_outside_self_intro(self) -> None:
        controller = self.make_controller(timeout=0.01)
        callback_called = asyncio.Event()

        async def callback(_result) -> None:
            callback_called.set()

        scheduled = await controller.schedule_self_intro_timeout(callback)
        await asyncio.sleep(0.03)

        self.assertFalse(scheduled)
        self.assertFalse(callback_called.is_set())
        self.assertEqual(controller.userdata.current_stage, InterviewState.NOT_STARTED)

    async def test_duplicate_transition_suppression(self) -> None:
        controller = self.make_controller()
        await controller.start_stage(InterviewState.SELF_INTRODUCTION)

        first = await controller.transition_to_past_experience(
            reason=TransitionReason.NORMAL_COMPLETION
        )
        second = await controller.transition_to_past_experience(
            reason=TransitionReason.TIMEOUT_FALLBACK
        )

        self.assertTrue(first.transitioned)
        self.assertFalse(second.transitioned)
        self.assertTrue(second.duplicate)
        self.assertEqual(controller.userdata.transition_reason, TransitionReason.NORMAL_COMPLETION)

    async def test_concurrent_transition_requests_only_one_wins(self) -> None:
        controller = self.make_controller()
        await controller.start_stage(InterviewState.SELF_INTRODUCTION)

        results = await asyncio.gather(
            controller.transition_to_past_experience(reason=TransitionReason.NORMAL_COMPLETION),
            controller.transition_to_past_experience(reason=TransitionReason.TIMEOUT_FALLBACK),
            controller.transition_to_past_experience(reason=TransitionReason.MANUAL_OVERRIDE),
        )

        self.assertEqual(sum(result.transitioned for result in results), 1)
        self.assertEqual(sum(result.duplicate for result in results), 2)
        self.assertEqual(controller.userdata.current_stage, InterviewState.PAST_EXPERIENCE)

    async def test_prompt_guard_behavior(self) -> None:
        controller = self.make_controller()

        first = await controller.mark_prompt_sent_if_needed(InterviewState.SELF_INTRODUCTION)
        second = await controller.mark_prompt_sent_if_needed(InterviewState.SELF_INTRODUCTION)
        other_stage = await controller.mark_prompt_sent_if_needed(InterviewState.PAST_EXPERIENCE)

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertTrue(other_stage)

    async def test_past_experience_timeout_advances_and_then_completes(self) -> None:
        controller = self.make_controller(past_timeout=0.01, max_past_timeouts=2)
        await controller.start_stage(InterviewState.SELF_INTRODUCTION)
        await controller.transition_to_past_experience(reason=TransitionReason.NORMAL_COMPLETION)
        await controller.activate_past_experience_area(PastExperienceArea.PROJECT_OVERVIEW)
        results = []

        async def callback(result) -> None:
            results.append(result)
            if not result.completed:
                await controller.schedule_past_exp_timeout(callback)

        scheduled = await controller.schedule_past_exp_timeout(callback)
        self.assertTrue(scheduled)

        await asyncio.wait_for(self._wait_for(lambda: len(results) == 2), timeout=1.0)

        self.assertEqual([result.timeout_count for result in results], [1, 2])
        self.assertEqual(results[0].skipped_area, PastExperienceArea.PROJECT_OVERVIEW)
        self.assertEqual(results[0].next_area, PastExperienceArea.TECH_STACK)
        self.assertEqual(results[1].skipped_area, PastExperienceArea.TECH_STACK)
        self.assertEqual(results[1].next_area, PastExperienceArea.BOTTLENECK)
        self.assertFalse(results[0].completed)
        self.assertTrue(results[1].completed)
        self.assertEqual(controller.userdata.current_stage, InterviewState.COMPLETED)
        self.assertTrue(controller.userdata.past_experience_completed)
        self.assertEqual(
            controller.userdata.past_experience_completion_reason,
            TransitionReason.TIMEOUT_FALLBACK,
        )

    async def test_past_experience_timeout_skips_active_area_without_repeating_it(self) -> None:
        controller = self.make_controller(past_timeout=0.01, max_past_timeouts=5)
        await controller.start_stage(InterviewState.SELF_INTRODUCTION)
        await controller.transition_to_past_experience(reason=TransitionReason.NORMAL_COMPLETION)
        await controller.activate_past_experience_area(PastExperienceArea.BOTTLENECK)
        results = []

        async def callback(result) -> None:
            results.append(result)

        self.assertTrue(await controller.schedule_past_exp_timeout(callback))
        await asyncio.wait_for(self._wait_for(lambda: len(results) == 1), timeout=1.0)

        self.assertEqual(results[0].skipped_area, PastExperienceArea.BOTTLENECK)
        self.assertNotEqual(results[0].next_area, PastExperienceArea.BOTTLENECK)
        self.assertEqual(results[0].next_area, PastExperienceArea.PROJECT_OVERVIEW)
        self.assertIn(PastExperienceArea.BOTTLENECK, controller.userdata.past_experience_skipped_areas)

    async def test_past_experience_timeout_not_scheduled_outside_past_experience(self) -> None:
        controller = self.make_controller(past_timeout=0.01)
        callback_called = asyncio.Event()

        async def callback(_result) -> None:
            callback_called.set()

        scheduled = await controller.schedule_past_exp_timeout(callback)
        await asyncio.sleep(0.03)

        self.assertFalse(scheduled)
        self.assertFalse(callback_called.is_set())

    async def test_completing_past_experience_cancels_timeout(self) -> None:
        controller = self.make_controller(past_timeout=0.05)
        await controller.start_stage(InterviewState.SELF_INTRODUCTION)
        await controller.transition_to_past_experience(reason=TransitionReason.NORMAL_COMPLETION)
        callback_called = asyncio.Event()

        async def callback(_result) -> None:
            callback_called.set()

        self.assertTrue(await controller.schedule_past_exp_timeout(callback))
        await controller.complete_past_experience(summary="done")
        await asyncio.sleep(0.08)

        self.assertFalse(callback_called.is_set())
        self.assertEqual(controller.userdata.current_stage, InterviewState.COMPLETED)
        self.assertEqual(controller.userdata.past_experience_summary, "done")
        self.assertEqual(
            controller.userdata.past_experience_completion_reason,
            TransitionReason.NORMAL_COMPLETION,
        )

    async def test_state_validation_rejects_invalid_transition(self) -> None:
        controller = self.make_controller()

        with self.assertRaises(StateTransitionError):
            await controller.transition_to_past_experience(
                reason=TransitionReason.NORMAL_COMPLETION
            )

    async def _wait_for(self, predicate) -> None:
        while not predicate():
            await asyncio.sleep(0.001)


if __name__ == "__main__":
    unittest.main()
