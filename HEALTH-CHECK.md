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

## Gameplay pass

- Wall-aware targeting, melee, skills, enemy strikes and ally attacks; arrows
  check their travelled segment, and enemy detection respects walls.
- Melee collision uses the post-lunge position. Last-frame parries preserve
  enemy/boss stun instead of immediately overwriting it with recovery.
- Skills cannot fire during stun/dash; dashing cancels charge. Pause clears the
  buffered attack, and scene restarts clear paths, flags and slowdown effects.
- Heavy attack button indicates charge/release; dash meter uses its real cooldown.
- Pickups avoid zero-distance division and wall drift, remain at full health,
  show actual healing, and cannot revive a player after a lethal hit that frame.
- Passive regeneration waits until nearby visible combat has ended.
- Retry and Continue restore chapter-entry corruption and choices using a
  validated checkpoint. Fresh games reset the tutorial hints too.
- Audio context resumes after a suspended start when an action initialises audio.

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

Added regression checks cover wall-blocked attacks, skill stun restrictions,
last-frame melee/boss parries, exact-position/full-health pickups, death ordering,
regeneration, charge/cooldown feedback, dash/pause cancellation, transient state
reset, checkpoint retries, fresh-script Continue and malformed checkpoint recovery.
Run `TEST_NO_WEBGL=1 node tests/health.cjs` for the unavailable-GPU startup case.

Story text, scene sequence, combat damage values and map layouts are unchanged.
Existing save keys are retained, with a new `gatebreak.checkpoint.v1` snapshot. The title and failure explanation can be inspected in-browser.

## Still needs hands-on verification

This environment's browser cannot create a WebGL context. A desktop attempt
confirmed the same limitation, so no complete visual play-through is claimed.
Check the published game on a WebGL device for character/scenery alignment,
phone touch ergonomics, frame rate, audio playback, encounter difficulty and
both portrait and landscape layouts. Browser font loading remains optional;
font failure falls back to the existing monospace style.
