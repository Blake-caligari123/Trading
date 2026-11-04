# Gatebreak

A 2.5D pixel-art action RPG prologue for mobile and desktop browsers. Chibi pixel characters stand in a lit 3D world, and the combat is timing-based.

Twin brothers. A little sister. A sword that was chained beneath the house for a reason.

## Play

Play at https://blake-caligari123.github.io/Trading/ in a modern browser with WebGL. The game engine and scenery are included locally; optional web fonts fall back to monospace.

To host it, enable **GitHub Pages** for this repository (Settings → Pages → deploy from branch, root folder) and open the URL it gives you.

## The prologue

Nine scenes: a sparring match at Ashford Estate, Aldric's farewell, the break-in, the whisper behind the bookcase, the sword Morrow, the Old Wood and the Sunken Temple, the ruins in the rain, and the battlefield years later. You play both twins: Kael in the yard and during the break-in, Riven from the whisper onward. Two choices in the temple carry forward into later dialogue and the end card. Progress saves at the start of each scene. Retrying or continuing restores that scene’s starting corruption and story choices.

## Controls

| Action | Touch | Keyboard |
|---|---|---|
| Move | Drag anywhere | WASD / arrows |
| Attack / talk | ATTACK (tap; buffered combos) | J or Space |
| Heavy attack | Hold ATTACK, release when the ring fills | Hold J |
| Guard / parry | Hold GUARD; tap as a blade flashes to parry | L or C |
| Dash / perfect dodge | DASH | K or Shift |
| Morrow skills | REND, WAVE, DEVOUR | U, I, O |
| Pause | II button | Esc or P |

## Combat basics

- **Stamina** (gold bar) is spent by swings, dashes and blocking.
- **Poise** (thin gold bar under enemy health): break it to stun an enemy and land critical hits.
- **Parry** melee attacks by tapping GUARD as the blade glints. **Perfect dodge** by dashing just before a hit lands.
- **Red ground** marks unblockable attacks. Dash out of them.
- Bandits raise their guard. Use a heavy attack to break it.
- As Riven, every kill and skill raises **corruption**: more damage, and a darker sprite.

## Tech

The game lives in `index.html`, with local engine and scenery assets. Three.js r128 renders the 3D world. All pixel art is generated procedurally in code at load time.

## Graphics pass

The Blender scenery pack adds a tiled, timber-framed manor, barrel, chest,
bench and altar. Ground details are baked at 32 pixels per tile, night lighting
is more readable, and cached display poses provide subtle strike/guard motion.
Story, collision maps, combat rules and save keys are unchanged.

- Rebuild models and the asset preview: `blender -b --python tools/build_assets.py`
- Validate: `node tests/graphics.cjs`
- Serve this directory with any static HTTP server and open `index.html`.
- Keep `assets/environment.js` alongside the HTML. If absent, the original
  primitive scenery is used. Mesh buffers are shared across map loads.
- `art/environment.blend` is editable source; `art/manor-preview.png` is a Blender
  asset render, not a gameplay screenshot.

Validation limitation: the review browser cannot create WebGL contexts. Title
loading, JS syntax, mesh data and preservation of the runtime/story were checked;
full gameplay and phone frame rate still need verification on a WebGL device.

## Health and polish

See [HEALTH-CHECK.md](HEALTH-CHECK.md) for fixes, tested behaviour and remaining
hands-on checks. Run `node tests/graphics.cjs` and `node tests/health.cjs`.
The engine is now included at `assets/vendor/three-r128.min.js`.

## Gameplay polish

Attacks and targeting respect walls. Successful parries retain their full punish
window, including against Hesk. Dash cancels attack charge, stunned characters
cannot cast skills, and the shorter input buffer reduces delayed swings.

The attack button shows CHARGE / RELEASE, and the dash meter reflects its actual
cooldown. Healing drops remain available at full health and report the amount
healed. Passive healing starts after disengaging from nearby enemies. Retry and
Continue restore the chapter-entry snapshot, including corruption and choices.
