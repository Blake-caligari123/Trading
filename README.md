# Gatebreak

A 2.5D pixel-art action RPG prologue for mobile and desktop browsers. Chibi pixel characters stand in a lit 3D world, and the combat is timing-based.

Twin brothers. A little sister. A sword that was chained beneath the house for a reason.

## Play

Open `index.html` in a modern browser. It needs WebGL and an internet connection the first time, to load Three.js and the fonts from a CDN.

To host it, enable **GitHub Pages** for this repository (Settings → Pages → deploy from branch, root folder) and open the URL it gives you.

## The prologue

Nine scenes: a sparring match at Ashford Estate, Aldric's farewell, the break-in, the whisper behind the bookcase, the sword Morrow, the Old Wood and the Sunken Temple, the ruins in the rain, and the battlefield years later. You play both twins: Kael in the yard and during the break-in, Riven from the whisper onward. Two choices in the temple carry forward into later dialogue and the end card. Progress saves at the start of each scene.

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

A single self-contained HTML file. Three.js r128 (from the cdnjs CDN) renders the 3D world. All pixel art is generated procedurally in code at load time.
