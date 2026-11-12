# Changelog

## 2.3.0

Protege GX discovery and area status. Includes the fork's timed door unlock (`ict_automation.unlock_for` and `ict_automation_door`).

### Discovery

The 2.0–2.2 scan started at record ID 1 and stopped after 3 empty IDs. On a GX panel that drops the front door (ID 0), stops before expander inputs when IDs 8–11 are empty, and never reaches outputs that start around ID 19.

- Scans start at ID 0.
- The gap is configurable. The default is 64 empty IDs. 0 scans the full range (doors and areas through 255, inputs and outputs through 511, trouble inputs through 255), one ID at a time so a NACK cannot be applied to the wrong record.
- Saved and hand-entered IDs are probed even past the gap. They are not treated as the complete list, which used to stop the next start from looking any further.
- Only a real "index not valid" NACK counts as an empty slot. A timeout is not proof the record is gone, and it does not delete a saved record.

### Area status

On GX the alarm panel stayed `unknown` back on 1.7.0, and 2.2.0 then failed to discover the area at all.

- Area status blocks shorter than the 2014 8-byte layout are decoded. 1.7.0 read `body[6]` inside a bare `except`, so a 5- or 6-byte reply was thrown away and the entity never left its initial `None` (Home Assistant shows that as `unknown`). 2.2.0 required 7 bytes, so the same reply never matched the status waiter, the probe timed out, and three timeouts ended the area scan. Area 1 was dropped even though the ID is not in a gap.
- If a status request is refused or times out, the record is read by subscribing with force-update, which is how this firmware sends area state. A logged-out failure is retried once while logged in.
- The periodic refresh no longer stops at the first error, so one area that does not answer cannot block doors and inputs.
- After monitoring is restored, any record that still has no status is read. Panels that ignore the force-update flag no longer leave the alarm panel unknown until the next minute.

### Names, manual IDs, migration

- The WX HTTPS operator flow does not apply to GX. A failed or blank operator login no longer blocks setup when the Automation and Control service answers. Names fall back to `Door 0`, `Area 1`, `Input CP001:02`. Rename them in Home Assistant.
- **Configure > Record IDs and discovery** pins IDs per type (`0`, `12-20`, `19=Garage light`). Pinned records are never removed by a scan.
- Migration from 1.x keeps the hand-entered records, their names, and their unique IDs (`ict_area_1`, `ict_door_0`, …). 2.0–2.2 deleted those entities; an install that already went through that migration cannot get them back from here.

## 2.2.0

Upstream release this fork is based on: web-only mode, automatic control-service detection, open zones, tamper and emergency alerts. See the upstream project for that history.
