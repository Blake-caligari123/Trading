# Gatebreak health check

## Fixed

- Pause now freezes the game clock, scene waits, fades and camera animation.
- Escape/P resumes as well as pauses. Pausing, losing focus and changing modes
  clears movement, guard and attack charge to avoid stuck controls.
- Action buttons capture their pointer and release on cancellation or capture
  loss. Keyboard-triggered button clicks release held actions immediately.
- Scene starts reset attack/dash cooldowns and temporary combat state.
- New prologues clear old choices and exploration flags. Invalid saved choice
  values (including JSON null) recover safely.
- Map changes dispose old ground textures, transient materials and geometry.
  Shared Blender model buffers and reusable materials remain cached.
- Three.js r128 is served locally, preserving its existing version and licence,
  so loading the engine no longer depends on an external CDN.
- Both start and continue are blocked when graphics cannot initialise, with a
  visible explanation rather than a nonfunctional continue button.

## Validation

Run `node tests/graphics.cjs` and `node tests/health.cjs` from the repository root.
The latter uses real Three.js geometry with stubbed DOM/canvas and GPU rendering.
It checks every map build, bounded texture cache across repeated loads, pause
and hidden-tab clocks, input reset, keyboard resume, corrupted saves, cooldown
reset, all nine scene starts, opening dialogue into sparring, stamina cost,
guard/dash and map-boundary collision. All four mercy/brother choice combinations
reach the ending and persist the expected choices in scripted progression.
Progression tests simulate combat outcomes and move the player to story triggers;
they do not establish that encounters are balanced or paths are easy to navigate.

Story text, scene sequence, combat damage values, map layouts, and save keys
are unchanged. The title and failure explanation can be inspected in-browser.

## Still needs hands-on verification

This environment's browser cannot create a WebGL context. A desktop attempt
confirmed the same limitation, so no complete visual play-through is claimed.
Check the published game on a WebGL device for character/scenery alignment,
phone touch ergonomics, frame rate, audio playback, encounter difficulty and
both portrait and landscape layouts. Browser font loading remains optional;
font failure falls back to the existing monospace style.
