# PTI Checker bot mark

Built in the Gürman Trucking visual language: one very thick stroke with round
caps, a thin negative-space groove down its centre, and the two dark-red dots
set diagonally — the umlaut of GÜRMAN, reused as the mark's signature. The form
is a checkmark, because the bot's whole job is pass/fail.

## Files

| File | Use |
| --- | --- |
| `pti-bot-avatar.svg` / `-512/128/48.png` | **Telegram bot avatar.** Full-bleed coral disc, survives the circular crop. |
| `pti-bot-appicon.svg` / `pti-bot-appicon-512.png` | Rounded-square variant, for app-icon slots and the Mini App. |
| `pti-bot-icon.svg` | Coral mark for **light** backgrounds — the groove is painted white. |
| `pti-bot-icon-512.png` | Same mark, transparent background, groove is a real hole. Safe over any colour. |
| `pti-bot-icon-white-512.png` | Same, flattened on white. |
| `pti-bot-lockup.png` / `-dark.png` | Horizontal lockup for the panel header and README. |

## Palette

| Role | Hex |
| --- | --- |
| Coral (gradient light → deep) | `#F76044` → `#E83426` |
| Dot red, on white | `#C9271D` |
| Dot red, on coral | `#C22218` |
| Slate (wordmark) | `#4C5F6C` |
| Sage (subtitle) | `#8FA5A3` |

## Regenerating

```bash
py -3.11 assets/logo/make_logo.py
```

Pillow only — geometry lives in one block of constants at the top of the script,
and the SVGs carry the same numbers by hand. The SVGs are deliberately
**mask-free**: the groove is a second stroke sampling the same
`gradientUnits="userSpaceOnUse"` ramp, so they render in tools with partial SVG
support instead of losing the check entirely.

The lockup's type is set in Segoe UI Black — a stand-in, not the fleet's actual
typeface. Pair the mark with the real `GÜRMAN` wordmark for anything customer-facing.
