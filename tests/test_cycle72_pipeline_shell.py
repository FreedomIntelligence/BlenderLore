from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "blender/scripts/run_total_asset_pipeline.sh"


class Cycle72PipelineShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SHELL.read_text(encoding="utf-8")

    def test_cycle72_entry_exposes_exact_lifecycle_commands(self) -> None:
        self.assertIn('cycle72_control "${2:-status}"', self.source)
        control = self.source[
            self.source.index("cycle72_control() {"):
            self.source.index("start_batch_all_servers() {")
        ]
        for action in ("start)", "status)", "checkpoint)", "drain)"):
            self.assertIn(action, control)

    def test_cycle72_production_topology_uses_replacement_node(self) -> None:
        self.assertIn(
            'SECONDARY_PORT="${TOTAL_ASSET_SECONDARY_PORT:-31722}"',
            self.source,
        )
        self.assertIn(
            'export TOTAL_ASSET_SECONDARY_PORT="31722"',
            self.source,
        )
        self.assertNotIn(
            'export TOTAL_ASSET_SECONDARY_PORT="${TOTAL_ASSET_SECONDARY_PORT:-31722}"',
            self.source,
        )

    def test_cycle72_forces_key_only_worker_transport(self) -> None:
        control = self.source[
            self.source.index("cycle72_load_private_environment() {"):
            self.source.index("start_batch_all_servers() {")
        ]
        self.assertIn("TOTAL_ASSET_ALLOW_LEGACY_PASSWORD_SSH=0", control)
        self.assertIn("total_asset_holder_guard_launchd.py install", control)
        self.assertIn("total_asset_source_prefetch_launchd.py install", control)
        self.assertIn("total_asset_remote_source_stager_launchd.py install", control)
        self.assertIn("total_asset_cycle72_launchd.py install", control)
        self.assertIn(
            '--node-preparation-limits "${TOTAL_ASSET_NODE_PREPARATION_LIMITS:-${SECONDARY_PORT}=4,${PORT}=4,${TERTIARY_PORT}=3}"',
            control,
        )
        self.assertIn(
            '--global-preparation-limit "${TOTAL_ASSET_GLOBAL_PREPARATION_LIMIT:-11}"',
            control,
        )
        self.assertIn(
            '--poll-seconds "${TOTAL_ASSET_CYCLE72_POLL_SECONDS:-30}"',
            control,
        )
        self.assertIn(
            '--poll-seconds "${TOTAL_ASSET_SOURCE_PREFETCH_POLL_SECONDS:-15}"',
            control,
        )

    def test_cycle72_installs_prefetch_before_supervisor_and_reports_status(self) -> None:
        control = self.source[
            self.source.index("cycle72_install_daemons() {"):
            self.source.index("start_batch_all_servers() {")
        ]
        self.assertLess(
            control.index("total_asset_source_prefetch_launchd.py install"),
            control.index("total_asset_remote_source_stager_launchd.py install"),
        )
        self.assertLess(
            control.index("total_asset_remote_source_stager_launchd.py install"),
            control.index("total_asset_cycle72_launchd.py install"),
        )
        self.assertIn("total_asset_source_prefetch.py status", control)
        self.assertIn("total_asset_remote_source_stager.py status", control)
        stager_install = control[
            control.index("total_asset_remote_source_stager_launchd.py install"):
            control.index("total_asset_cycle72_launchd.py install")
        ]
        self.assertIn('--identity-file "$TOTAL_ASSET_PREFLIGHT_SSH_KEY"', stager_install)
        self.assertIn('--known-hosts "$TOTAL_ASSET_SSH_KNOWN_HOSTS"', stager_install)
        self.assertIn('--remote-port "$PORT"', stager_install)
        self.assertNotIn("remote_source_stager_launchd.py kickstart", stager_install)

    def test_cycle72_ignores_stale_ambient_project_override(self) -> None:
        prefix = self.source[: self.source.index("case \"$COMMAND\" in")]
        self.assertIn('SCRIPT_PROJECT="$(cd "$SCRIPT_DIR/../.." && pwd)"', prefix)
        self.assertIn('if [ "$COMMAND" = "cycle72" ]; then', prefix)
        self.assertIn('PROJECT="$SCRIPT_PROJECT"', prefix)

    def test_cycle72_precisely_retires_legacy_watchdogs_before_install(self) -> None:
        control = self.source[
            self.source.index("retire_exact_screen_formal_slot_watchdog() {"):
            self.source.index("cycle72_control() {")
        ]
        self.assertIn('name="total_asset_formal_slot_watchdog"', control)
        self.assertIn('total_asset_slot_watchdog.py', control)
        self.assertIn('executable.startswith("python")', control)
        holder = self.source[
            self.source.index("retire_exact_screen_holder_guard() {"):
            self.source.index("retire_exact_screen_formal_slot_watchdog() {")
        ]
        exact_descendant = self.source[
            self.source.index("cycle72_screen_python_descendant() {"):
            self.source.index("retire_exact_screen_python_daemon() {")
        ]
        self.assertIn('executable.startswith("python")', exact_descendant)
        self.assertIn("retire_exact_screen_python_daemon", holder)
        self.assertNotIn("pkill", control)
        self.assertNotIn("killall", control)
        install = control[control.index("cycle72_install_daemons() {"):]
        self.assertLess(
            install.index("retire_exact_screen_formal_slot_watchdog"),
            install.index("total_asset_holder_guard_launchd.py install"),
        )

    def test_launchd_handoff_is_staged_and_retires_only_screen_descendants(self) -> None:
        control = self.source[
            self.source.index("cycle72_require_fresh_holder_evidence() {"):
            self.source.index("cycle72_control() {")
        ]
        install = control[control.index("cycle72_install_daemons() {"):]
        holder_install = install.index("total_asset_holder_guard_launchd.py install")
        holder_retire = install.index("retire_exact_screen_holder_guard")
        holder_kickstart = install.index("total_asset_holder_guard_launchd.py kickstart")
        supervisor_install = install.index("total_asset_cycle72_launchd.py install")
        supervisor_retire = install.index("retire_exact_screen_cycle72_supervisor")
        supervisor_kickstart = install.index("total_asset_cycle72_launchd.py kickstart")
        self.assertLess(holder_install, holder_retire)
        self.assertLess(holder_retire, holder_kickstart)
        self.assertLess(holder_kickstart, supervisor_install)
        self.assertLess(supervisor_install, supervisor_retire)
        self.assertLess(supervisor_retire, supervisor_kickstart)
        self.assertIn("members.issubset(descendants)", control)
        self.assertIn('"total_asset_cycle72_supervisor.py" "run"', control)
        self.assertIn("cycle72_require_fresh_holder_evidence", control)
        self.assertIn("(30808, 3)", control)
        self.assertNotIn("pkill", control)
        self.assertNotIn("killall", control)

    def test_nonzero_screen_listing_cannot_abort_exact_retirement(self) -> None:
        control = self.source[
            self.source.index("retire_exact_screen_python_daemon() {"):
            self.source.index("cycle72_control() {")
        ]
        self.assertEqual(
            control.count("{ screen -ls 2>/dev/null || true; }"),
            3,
        )

    def test_holder_cutover_evidence_requires_all_11_slots_and_reserve(self) -> None:
        function = self.source[
            self.source.index("cycle72_require_fresh_holder_evidence() {"):
            self.source.index("cycle72_screen_python_descendant() {")
        ]
        program = function.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        boot = {
            30773: "27712927-0e91-499a-b646-2431d9e5daec",
            30808: "c61501db-0d6e-4dac-9866-cd582b4e19cc",
            31722: "c61501db-0d6e-4dac-9866-cd582b4e19cc",
        }
        expected = [
            *((31722, gpu, gpu) for gpu in range(4)),
            *((30773, gpu, gpu + 4) for gpu in range(4)),
            *((30808, gpu, gpu + 8) for gpu in range(3)),
        ]
        payload = {
            "schema": "video2blender.total-asset-holder-guard-state.v2",
            "status": "healthy",
            "observed_at_epoch": time.time(),
            "reachable_ports": [30773, 30808, 31722],
            "nodes": [
                {
                    "remote_port": port,
                    "boot_id": boot[port],
                    "tcp_reachable": True,
                    "probe_error": None,
                }
                for port in sorted(boot)
            ],
            "events": [
                {
                    "remote_port": port,
                    "gpu": gpu,
                    "worker_index": worker,
                    "action": "holder_ready",
                    "boot_id": boot[port],
                }
                for port, gpu, worker in expected
            ],
            "reserve_events": [{
                "remote_port": 30808,
                "gpu": 3,
                "action": "holder_ready",
                "boot_id": boot[30808],
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "guard.json"
            state.write_text(json.dumps(payload), encoding="utf-8")
            valid = subprocess.run(
                [sys.executable, "-", str(state), "45"],
                input=program,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)
            payload["reserve_events"] = []
            state.write_text(json.dumps(payload), encoding="utf-8")
            missing_reserve = subprocess.run(
                [sys.executable, "-", str(state), "45"],
                input=program,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(missing_reserve.returncode, 75)

    def test_status_path_does_not_install_or_start_daemons(self) -> None:
        control = self.source[
            self.source.index("cycle72_control() {"):
            self.source.index("start_batch_all_servers() {")
        ]
        status_branch = control[control.index("    status)"):control.index("    checkpoint)")]
        self.assertNotIn("install_daemons", status_branch)
        self.assertIn("total_asset_cycle72.py status", status_branch)
        self.assertIn("total_asset_work_buffer.py", status_branch)
        self.assertIn("total_asset_supply_refill.py status", status_branch)

    def test_highqal_cli_exposes_decision_complete_lifecycle_without_fixed_count(self) -> None:
        control = self.source[
            self.source.index("highqal_priority_control() {"):
            self.source.index("cycle72_control() {")
        ]
        usage = (
            "{plan|prepare|start|retry-canary|status|pause|resume|checkpoint|drain|discover}"
        )
        self.assertIn(usage, control)
        self.assertIn('python3 blender/scripts/highqal_source_priority.py "$action"', control)
        self.assertNotIn("--expected-wave1", control)

    def test_highqal_status_is_one_json_and_checkpoint_uses_one_stable_artifact(self) -> None:
        prefix = self.source[:self.source.index("select_task1_evidence_log() {")]
        self.assertIn(
            'HIGHQAL_CHECKPOINT="${HIGHQAL_CHECKPOINT:-$HIGHQAL_LOG_ROOT/highqal_priority.current.checkpoint.json}"',
            prefix,
        )
        control = self.source[
            self.source.index("highqal_priority_control() {"):
            self.source.index("cycle72_control() {")
        ]
        read_only = control[
            control.index("    plan|status|checkpoint)"):
            control.index("    prepare)")
        ]
        self.assertEqual(
            read_only.count("python3 blender/scripts/highqal_source_priority.py"), 1
        )
        self.assertIn('--reference-download-state "$HIGHQAL_DOWNLOAD_STATE"', read_only)
        self.assertIn('--canary-promotion-state "$HIGHQAL_CANARY_PROMOTION_STATE"', read_only)
        self.assertIn('--checkpoint-path "$HIGHQAL_CHECKPOINT"', read_only)
        self.assertNotIn('json.dumps({"reference_download"', read_only)
        self.assertNotIn('json.dumps({"canary_promotion"', read_only)

    def test_highqal_cutover_migrates_before_priority_registration(self) -> None:
        control = self.source[
            self.source.index("highqal_priority_control() {"):
            self.source.index("cycle72_control() {")
        ]
        start = control[control.index("    start)"):control.index("    pause)")]
        prepare = start.index("highqal_source_priority.py prepare")
        cutover = start.index("cycle72_safe_priority_runtime_cutover")
        first_registration = start[
            start.index('if [ -z "$current_generation" ]'):
            start.index('if [ "$current_generation" != "$canary_generation" ]')
        ]
        register = first_registration.index("highqal_source_priority.py start")
        resume = start.rindex("highqal_source_priority.py resume")
        promotion = start.index("start_highqal_canary_promotion")
        self.assertLess(prepare, cutover)
        self.assertGreater(register, 0)
        self.assertLess(resume, promotion)
        self.assertIn("--initial-state paused", start)
        self.assertIn('--manifest "$HIGHQAL_CANARY_MANIFEST"', start)
        self.assertNotIn("cycle72_install_daemons", start)

    def test_highqal_cutover_uses_correct_state_migration_and_restore_order(self) -> None:
        control = self.source[
            self.source.index("cycle72_require_priority_runtime() {"):
            self.source.index("write_highqal_download_state() {")
        ]
        self.assertIn('$state_root/supervisor_state.json', control)
        self.assertNotIn('$state_root/supervisor.json', control)
        cutover = control[
            control.index("cycle72_safe_priority_runtime_cutover() {"):
        ]
        install = cutover.index("cycle72_install_priority_supervisor_only")
        migrate = cutover.index("migrate-priority-schema")
        attest = cutover.index("cycle72_require_priority_runtime")
        self.assertLess(install, migrate)
        self.assertLess(migrate, attest)
        self.assertIn("cycle72_restore_previous_supervisor", cutover)
        self.assertIn("migration-backup-dir", cutover)

    def test_priority_runtime_attestation_requires_fresh_live_lock_owner(self) -> None:
        control = self.source[
            self.source.index("cycle72_require_priority_runtime() {"):
            self.source.index("highqal_cutover_lock_is_held() {")
        ]
        self.assertIn("/bin/launchctl print", control)
        self.assertIn("supervisor.lock", control)
        self.assertIn("observed_at_epoch", control)
        self.assertIn("TOTAL_ASSET_PRIORITY_RUNTIME_MAX_AGE_SECONDS", control)
        self.assertIn("fcntl.LOCK_EX | fcntl.LOCK_NB", control)
        self.assertIn("raise SystemExit(75)", control)

    def test_highqal_long_lived_screens_drop_the_inherited_cutover_lock(self) -> None:
        helper = self.source[
            self.source.index("highqal_spawn_without_cutover_lock() {"):
            self.source.index("cycle72_priority_schema_version() {")
        ]
        self.assertIn("os.close(int(fd_text))", helper)
        self.assertIn('"HIGHQAL_CUTOVER_LOCK_HELD"', helper)
        self.assertIn('"TOTAL_ASSET_SCHEDULER_LOCK_FD"', helper)
        downloader = self.source[
            self.source.index("start_highqal_reference_download() {"):
            self.source.index("highqal_manifest_generation() {")
        ]
        promotion = self.source[
            self.source.index("start_highqal_canary_promotion() {"):
            self.source.index("highqal_priority_control() {")
        ]
        self.assertIn("highqal_spawn_without_cutover_lock screen", downloader)
        self.assertIn("highqal_spawn_without_cutover_lock screen", promotion)

    def test_highqal_canary_promotes_only_after_exact_acceptance_and_adoption(self) -> None:
        control = self.source[
            self.source.index("highqal_canary_boundary_state() {"):
            self.source.index("highqal_priority_control() {")
        ]
        self.assertIn('counts.get("ready", 0)', control)
        self.assertIn('counts.get("leased", 0)', control)
        self.assertIn('print("accepted")', control)
        promote = control[
            control.index("promote_highqal_canary_now() {"):
            control.index("run_highqal_canary_promotion_loop() {")
        ]
        receipt = promote.index("highqal_source_priority.py adopt-canary")
        register = promote.index("highqal_register_full_manifest_paused")
        adopt = promote.index("highqal_apply_canary_adoption_to_current_full")
        resume = promote.index("highqal_source_priority.py resume")
        self.assertLess(receipt, register)
        self.assertLess(register, adopt)
        self.assertLess(adopt, resume)
        adoption_helper = control[
            control.index("highqal_apply_canary_adoption_to_current_full() {"):
            control.index("reconcile_highqal_full_generation_now() {")
        ]
        self.assertIn("adopt_priority_canary_receipt", adoption_helper)

    def test_current_full_generation_is_reconciled_before_resume(self) -> None:
        control = self.source[
            self.source.index("highqal_priority_control() {"):
            self.source.index("cycle72_control() {")
        ]
        start = control[control.index("    start)"):control.index("    pause)")]
        branch = start[
            start.index('if [ "$current_generation" = "$full_generation" ]'):
            start.index('if [ -z "$current_generation" ]')
        ]
        self.assertIn("reconcile_highqal_full_generation_now", branch)
        self.assertNotIn("highqal_source_priority.py resume", branch)
        self.assertIn("refusing to abandon or overwrite", start)

    def test_highqal_mutations_use_an_independent_inherited_cutover_lock(self) -> None:
        lock = self.source[
            self.source.index("highqal_cutover_lock_is_held() {"):
            self.source.index("cycle72_priority_schema_version() {")
        ]
        self.assertIn("highqal-priority.lock", lock)
        self.assertIn("TOTAL_ASSET_SCHEDULER_LOCK_FD", lock)
        self.assertIn("st_dev", lock)
        control = self.source[
            self.source.index("highqal_priority_control() {"):
            self.source.index("cycle72_control() {")
        ]
        self.assertIn(
            "prepare|start|retry-canary|pause|resume|drain|discover|_promote-canary-now",
            control,
        )
        self.assertIn("_reconcile-full|_register-wave|_register-wave2)", control)

    def test_highqal_canary_and_final_paths_are_environment_parameterized(self) -> None:
        prefix = self.source[:self.source.index("select_task1_evidence_log() {")]
        for variable in (
            "HIGHQAL_LOG_ROOT",
            "HIGHQAL_MANIFEST",
            "HIGHQAL_STATUS",
            "HIGHQAL_FINAL_ROOT",
            "HIGHQAL_EVIDENCE_ROOT",
            "HIGHQAL_CANARY_ROOT",
            "HIGHQAL_CANARY_MANIFEST",
            "HIGHQAL_CANARY_STATUS",
            "HIGHQAL_CANARY_EVIDENCE_ROOT",
        ):
            self.assertIn(f'${{{variable}:-', prefix)

    def test_highqal_canary_v2_uses_locked_scene_audited_sample_set(self) -> None:
        prefix = self.source[:self.source.index("select_task1_evidence_log() {")]
        self.assertIn("highqal_source_work_manifest.canary.v2.json", prefix)
        expected_ids = (
            "bili_BV18AzmY9EHm_0a9cf2d20f--ac1b7905c0f52dd6",
            "bili_BV1tj411u773_45883adc15--b3ca241d5176c298",
            "bili_BV16kSpBdECz_4ddfe82a35--40850e417a939601",
        )
        for work_item_id in expected_ids:
            self.assertIn(f'readonly HIGHQAL_CANARY_', prefix)
            self.assertIn(f'="{work_item_id}"', prefix)
            self.assertEqual(self.source.count(f'--canary-work-item-id "$HIGHQAL_CANARY_'), 9)
        self.assertEqual(self.source.count('--canary-role-evidence "$HIGHQAL_CANARY_'), 9)
        self.assertNotIn('HIGHQAL_CANARY_STATIC_WORK_ITEM_ID="${', prefix)
        self.assertNotIn('HIGHQAL_CANARY_DYNAMIC_WORK_ITEM_ID="${', prefix)
        self.assertNotIn('HIGHQAL_CANARY_NONBLEND_WORK_ITEM_ID="${', prefix)
        for evidence in (
            "renderable=37",
            "no_material=0",
            "missing_image=0",
            "missing_library=0",
            "undefined_node=0",
            "source animated/armature/particle=0",
            "existing render.png visually verified as a complete cactus asset",
            "animated Armature and CAM_ROOT",
            "armature_count=1",
            "existing final_effect.mp4 visually verified with visible character motion",
        ):
            self.assertIn(evidence, prefix)

    def test_highqal_candidate_discovery_is_bounded_and_explicit(self) -> None:
        control = self.source[
            self.source.index("highqal_priority_control() {"):
            self.source.index("cycle72_control() {")
        ]
        discover = control[control.index("    discover)"):control.index("    _download-loop)")]
        self.assertIn('local discover_action="${1:-plan}"', discover)
        self.assertIn("plan|status|run", discover)
        self.assertIn('--candidate-json)', discover)
        self.assertIn('discover_action="run"', discover)
        self.assertIn("highqal_candidate_supply.py", discover)
        self.assertIn('--state-root "$HIGHQAL_CANDIDATE_SUPPLY_ROOT"', discover)

    def test_reference_downloader_has_heartbeat_resume_and_wave2_boundary_hook(self) -> None:
        control = self.source[
            self.source.index("write_highqal_download_state() {"):
            self.source.index("highqal_priority_control() {")
        ]
        self.assertIn("highqal-reference-download-state.v1", control)
        self.assertNotIn("awk 'END {print NR > 0 ? NR - 1 : 0}'", control)
        self.assertIn("awk 'END {print (NR > 0 ? NR - 1 : 0)}'", control)
        self.assertIn('"resume_policy": "yt_dlp_external_archive_v1"', control)
        self.assertIn("waiting_previous_wave", control)
        self.assertIn('--wave "$wave"', control)
        self.assertIn('previous_args+=(--previous-manifest', control)
        self.assertIn("--initial-state paused", control)
        boundary = control[
            control.index("highqal_workload_terminal_for_manifest() {"):
            control.index("prepare_and_register_highqal_wave() {")
        ]
        self.assertIn('("accepted", "needs_review", "failed")', boundary)
        self.assertIn('counts.get("blocked")', boundary)
        downloader = control[
            control.index("run_highqal_reference_download_loop() {"):
            control.index("start_highqal_reference_download() {")
        ]
        self.assertIn('_register-wave "$wave"', downloader)
        self.assertIn('register_rc" -ne 74', downloader)
        self.assertIn("successful files are retained", downloader)
        self.assertIn("--rewrite-pending-input", downloader)

    def test_later_wave_registration_is_strict_and_manual_pause_is_sticky(self) -> None:
        control = self.source[
            self.source.index("highqal_workload_terminal_for_manifest() {"):
            self.source.index("run_highqal_reference_download_loop() {")
        ]
        register = control[
            control.index("prepare_and_register_highqal_wave() {"):
            control.index("prepare_and_register_highqal_wave2() {")
        ]
        terminal = register.index("highqal_workload_terminal_for_manifest")
        plan = register.index('highqal_source_priority.py plan')
        prepare = register.index('highqal_source_priority.py prepare')
        self.assertLess(terminal, plan)
        self.assertLess(plan, prepare)
        self.assertIn("highqal_resume_is_registration_recovery", register)
        self.assertIn("paused_by_operator", register)
        self.assertIn("registering_wave", register)
        self.assertIn("return 76", register)

    def test_status_and_checkpoint_follow_the_registered_generation(self) -> None:
        control = self.source[
            self.source.index("highqal_priority_control() {"):
            self.source.index("cycle72_control() {")
        ]
        read_only = control[
            control.index("    plan|status|checkpoint)"):
            control.index("    prepare)")
        ]
        self.assertIn("highqal_current_priority_manifest_path", read_only)
        self.assertIn('--manifest "$observed_manifest"', read_only)

    def test_downloader_restart_resumes_after_the_current_registered_wave(self) -> None:
        control = self.source[
            self.source.index("highqal_reference_resume_wave() {"):
            self.source.index("start_highqal_reference_download() {")
        ]
        resume = control[
            control.index("highqal_reference_resume_wave() {"):
            control.index("run_highqal_reference_download_loop() {")
        ]
        downloader = control[control.index("run_highqal_reference_download_loop() {"):]
        self.assertIn('manifest = load_manifest(manifest_path)', resume)
        self.assertIn('print(max(2, wave + 1))', resume)
        self.assertIn('wave="$(highqal_reference_resume_wave)"', downloader)
        self.assertNotIn("local wave=2", downloader)

    def test_paused_registration_token_survives_and_public_start_can_recover_it(self) -> None:
        downloader = self.source[
            self.source.index("run_highqal_reference_download_loop() {"):
            self.source.index("start_highqal_reference_download() {")
        ]
        self.assertIn("paused registration retained for exact recovery", downloader)
        token_check = downloader.index("highqal_resume_is_registration_recovery")
        blocked_write = downloader.index("write_highqal_download_state blocked", token_check)
        self.assertLess(token_check, blocked_write)

        control = self.source[
            self.source.index("highqal_priority_control() {"):
            self.source.index("cycle72_control() {")
        ]
        start = control[control.index("    start)"):control.index("    pause)")]
        self.assertIn('registering_wave', start)
        self.assertIn('prepare_and_register_highqal_wave "$current_wave"', start)
        self.assertIn("explicitly paused; use highqal-priority resume", start)

    def test_operator_pause_intent_precedes_database_pause(self) -> None:
        control = self.source[
            self.source.index("highqal_priority_control() {"):
            self.source.index("cycle72_control() {")
        ]
        pause = control[control.index("    pause)"):control.index("    resume)")]
        intent = pause.index("pausing_by_operator")
        database_pause = pause.index("highqal_source_priority.py pause")
        complete = pause.rindex("write_highqal_download_state paused_by_operator")
        self.assertLess(intent, database_pause)
        self.assertLess(database_pause, complete)

    def test_canary_and_full_auto_resume_require_exact_registration_tokens(self) -> None:
        helper = self.source[
            self.source.index("reconcile_highqal_full_generation_now() {"):
            self.source.index("run_highqal_canary_promotion_loop() {")
        ]
        self.assertIn("registering_full", helper)
        self.assertIn("highqal_canary_promotion_is_registration_recovery", helper)
        self.assertIn("full Highqal Wave 1 remains explicitly paused", helper)
        control = self.source[
            self.source.index("highqal_priority_control() {"):
            self.source.index("cycle72_control() {")
        ]
        start = control[control.index("    start)"):control.index("    pause)")]
        self.assertIn("registering_canary", start)
        self.assertIn("highqal_canary_promotion_is_registration_recovery", start)
        self.assertIn("Highqal canary remains explicitly paused", start)

    def test_retry_canary_is_locked_atomic_and_crash_recoverable(self) -> None:
        helper = self.source[
            self.source.index("retry_highqal_canary() {"):
            self.source.index("highqal_priority_control() {")
        ]
        prepare = helper.index("highqal_source_priority.py prepare")
        intent = helper.index("retry_registering_canary")
        replace = helper.index("retry-canary-register")
        resume = helper.index("highqal_source_priority.py resume")
        promotion = helper.index("start_highqal_canary_promotion")
        self.assertLess(prepare, intent)
        self.assertLess(intent, replace)
        self.assertLess(replace, resume)
        self.assertLess(resume, promotion)
        self.assertIn("highqal_cutover_lock_is_held", helper)
        self.assertIn("highqal_retry_terminal_generation", helper)
        self.assertIn("exact_screen_count highqal_canary_promotion", helper)
        self.assertIn('--expected-current-generation "$old_generation"', helper)
        self.assertIn(
            '"$generation" retry_registering_canary', helper
        )
        self.assertNotIn("sqlite3.connect", helper)
        self.assertNotIn("UPDATE priority_", helper)

    def test_retry_paths_are_recovered_from_registered_manifest_with_stable_state(self) -> None:
        path_helpers = self.source[
            self.source.index("highqal_select_retry_paths_for_slug() {"):
            self.source.index("highqal_retry_terminal_generation() {")
        ]
        self.assertIn("highqal_adopt_registered_retry_paths", path_helpers)
        self.assertIn("highqal_source_work_manifest.canary.${slug}.json", path_helpers)
        self.assertIn(
            'f"highqal_source_work_manifest.wave0001.{knowledge}.json"',
            path_helpers,
        )
        self.assertIn("is_retry_canary", path_helpers)
        self.assertIn("is_retry_full", path_helpers)
        self.assertIn(
            "except (OSError, RuntimeError, sqlite3.Error):\n"
            "    # An unreadable/locked/malformed authority is not evidence",
            path_helpers,
        )
        authority_failure = path_helpers.index(
            "except (OSError, RuntimeError, sqlite3.Error):"
        )
        self.assertIn(
            "raise SystemExit(75)",
            path_helpers[authority_failure : authority_failure + 500],
        )
        self.assertIn(
            'if status.get("registered") is not True:\n'
            "    raise SystemExit(1)",
            path_helpers,
        )
        self.assertIn("registered manifest escapes Highqal roots", path_helpers)
        self.assertIn(
            'manifest.get("generation") or "") != str(status.get("generation")',
            path_helpers,
        )
        self.assertNotIn("HIGHQAL_CANARY_PROMOTION_STATE=", path_helpers)
        control = self.source[
            self.source.index("highqal_priority_control() {"):
            self.source.index("cycle72_control() {")
        ]
        self.assertLess(
            control.index("highqal_adopt_registered_retry_paths"),
            control.index("highqal_reexec_with_cutover_lock"),
        )
        self.assertIn(
            'if [ "$retry_path_rc" -ne 0 ] && [ "$retry_path_rc" -ne 1 ]; then',
            control,
        )

    def test_retry_path_authority_read_error_returns_75_before_resume(self) -> None:
        helper = self.source[
            self.source.index("highqal_adopt_registered_retry_paths() {"):
            self.source.index("highqal_retry_terminal_generation() {")
        ]
        heredoc = "<<'PY'\n"
        program_start = helper.index(heredoc) + len(heredoc)
        program_end = helper.index("\nPY\n", program_start)
        program = helper[program_start:program_end]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "blender" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "highqal_source_priority.py").write_text(
                "def load_manifest(_path):\n"
                "    raise AssertionError('manifest must not be read')\n",
                encoding="utf-8",
            )
            (scripts / "total_asset_cycle72.py").write_text(
                "def priority_workload_status(**_kwargs):\n"
                "    raise RuntimeError('transient authority read failure')\n",
                encoding="utf-8",
            )
            log_root = root / "logs"
            canary_root = root / "canary"
            log_root.mkdir()
            canary_root.mkdir()
            result = subprocess.run(
                [
                    sys.executable,
                    "-",
                    str(root / "leases.sqlite3"),
                    str(log_root),
                    str(canary_root),
                ],
                input=program,
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 75, result.stderr)

    def test_canary_promotion_monitor_cannot_bypass_operator_pause(self) -> None:
        boundary = self.source[
            self.source.index("highqal_canary_boundary_state() {"):
            self.source.index("highqal_register_full_manifest_paused() {")
        ]
        self.assertIn('status.get("state") == "paused"', boundary)
        self.assertIn('print("paused")', boundary)
        monitor = self.source[
            self.source.index("run_highqal_canary_promotion_loop() {"):
            self.source.index("start_highqal_canary_promotion() {")
        ]
        self.assertIn("      paused)", monitor)
        self.assertIn("return 0", monitor[monitor.index("      paused)"):])

    def test_canary_promotion_waiting_state_retains_exact_generation(self) -> None:
        monitor = self.source[
            self.source.index("run_highqal_canary_promotion_loop() {"):
            self.source.index("start_highqal_canary_promotion() {")
        ]
        self.assertIn(
            'canary_generation="$(highqal_manifest_generation '
            '"$HIGHQAL_CANARY_MANIFEST")"',
            monitor,
        )
        pending = monitor[
            monitor.index("      pending)"):
            monitor.index("      paused)")
        ]
        self.assertIn(
            '"waiting for exact canary terminal results" \\\n'
            '          "$canary_generation"',
            pending,
        )

    def test_operator_pause_invalidates_canary_recovery_before_database_pause(self) -> None:
        control = self.source[
            self.source.index("highqal_priority_control() {"):
            self.source.index("cycle72_control() {")
        ]
        pause = control[control.index("    pause)"):control.index("    resume)")]
        invalidate = pause.index("highqal_canary_promotion_state paused_by_operator")
        database_pause = pause.index("highqal_source_priority.py pause")
        self.assertLess(invalidate, database_pause)

    def test_explicit_full_resume_adopts_canary_before_exposing_ready_items(self) -> None:
        control = self.source[
            self.source.index("highqal_priority_control() {"):
            self.source.index("cycle72_control() {")
        ]
        resume = control[control.index("    resume)"):control.index("    drain)")]
        detect_full = resume.index('if [ "$generation" = "$full_generation" ]')
        adopt = resume.index("highqal_apply_canary_adoption_to_current_full")
        activate = resume.index("highqal_source_priority.py resume")
        self.assertLess(detect_full, adopt)
        self.assertLess(adopt, activate)
        self.assertIn("operator resume completed canary adoption", resume)


if __name__ == "__main__":
    unittest.main()
