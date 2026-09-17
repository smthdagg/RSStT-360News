# AI portal updater TDD evidence

The user journeys were derived during this implementation: parse public RSS/Atom feeds safely, publish a valid deduplicated snapshot, and keep the last good snapshot when all upstreams fail.

| Guarantee | Test | Result |
|---|---|---|
| RSS markup and dates are normalized | `test_parse_rss_removes_markup_and_normalizes_date` | PASS |
| Atom link attributes are supported | `test_parse_atom_uses_link_attribute` | PASS |
| Duplicate links are removed across sources | `test_build_payload_deduplicates_and_keeps_latest_categories` | PASS |
| JSON is replaced atomically | `test_write_payload_replaces_file_with_valid_json` | PASS |
| A failed refresh preserves the previous snapshot | `test_refresh_keeps_previous_snapshot_when_all_sources_fail` | PASS |
| A successful refresh writes a new snapshot | `test_refresh_writes_a_successful_source_snapshot` | PASS |

RED was confirmed before implementation with `ModuleNotFoundError: No module named 'update_portal'`. GREEN was confirmed with `python3 -m unittest -v test_update_portal.py` (6 tests passed).

Coverage tooling was checked but the bundled Python has no `coverage` module. The intentional gap is live HTTP behavior; the production timer smoke test covers all seven configured sources on the VPS.

The rebrand and port-routing regression checks are in `test_site_design.py`: the page contains the OpenAIClub brand and embedded SVG logo, and the HTTP 80 route serves `/downloads` directly. Both checks pass.

The latest layout check also asserts explicit wide (`min-width:1200px`) and narrow (`max-width:620px`) modes plus the 64×64 viewBox / larger logo treatment.

The narrow-layout regression also checks that the Hero grid and signal rows can shrink instead of creating horizontal overflow on mobile.
