# PLA-473 ordinary Quit regression — 2026-09-06

Reviewed finding: Linear comment 4db3d903-203a-4c04-82ca-7e9e4ddb01b3, PR74 head 06f9a874b06e3ec9203a4644b9eca1530e89a2d9.

Changed source: src-tauri/src/lib.rs only (handler + unit tests).

Extracted the actual Tauri event registration's signal/scheduling boundary into handle_exit_request, preserving the old behavior first. The regression ordinary_quit_allows_its_cleanup_triggered_final_exit then failed: prevented count was 2 instead of 1 after the real AppState::shutdown worker completed and the final code-0 callback was delivered. See quit-regression-before.log.

Fix: completed shutdown now returns without calling prevent_exit. While cleanup is in progress, repeat requests remain vetoed without scheduling a second worker. Cleanup failure now logs the failure, keeps the completion flag false, resets quitting to allow retry, and presents a native error dialog; it does not request final exit or falsely label failure as completion.

Three focused regressions pass: initial ordinary Quit followed by final code-0 admission; repeated requests while the actual lifecycle mutex blocks cleanup with preservation of the first requested exit code; actual shutdown returning a poisoned-mutex error followed by repair and explicit retry. See quit-regression-after.log. Full Rust/all-target and Clippy outputs are in quit-all-rust-tests.log and quit-clippy.log.

Test boundary: the tests invoke the exact helper called by the registered event closure and run the actual AppState shutdown worker, with observable prevent/final-exit/error callbacks. They do not launch a GUI or pretend to execute Tauri's ordinary app.exit route. Locked Tauri2.11.5's supplied MockRuntime::request_exit is unimplemented, so it cannot honestly supply that runtime test. Packaged acceptance must rebuild and verify real macOS menu/keyboard Quit/relaunch, owned PID disappearance, and updater/restore restart separately. Tauri ExitRequestApi::prevent_exit ignores the special restart code, unlike ordinary Quit; these tests do not certify restart behavior.

Remote delivery: the follow-up PR handoff and draft review candidate receipts record final exact source, CI and native observations. Source tests alone do not certify the native route. No Developer ID/notarization, real installed N-to-N+1 or public release pass is claimed.

## Quit/install race follow-up


The independent review identified an additional gap: updater replacement ran outside the lifecycle mutex after backend stop, so ordinary Quit could authorize final exit between the supported installer's bundle renames. The actual install call now goes through AppState::replace_application inside spawn_blocking. It holds the same lifecycle mutex as shutdown, checks that Quit is not already latched and that the backend is stopped under the update guard, then runs the supported installer. Native event handling still only latches/prevents Quit and schedules shutdown; it never waits for this mutex on the main thread.

Two additional before/after regressions exercise the production replacement/exit boundaries: a barrier holds application mutation open while Quit is requested (final exit must remain pending until release), and an already-latched Quit must reject the replacement closure. Both fail before the guard and pass after. Logs: quit-install-race-before.log, quit-install-race-after.log, quit-install-race-clippy.log, quit-install-race-all-tests.log. This does not replace installed native Quit/update acceptance.

After both repairs, **50 native library plus 6 example tests** and strict Clippy pass.
