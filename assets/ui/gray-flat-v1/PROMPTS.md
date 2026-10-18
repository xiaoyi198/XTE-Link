# Logo prompts

Mode: built-in image_gen.imagegen (not CLI).

## Initial monochrome concept

Use case: logo-brand
Asset type: a single small monochrome app mark for XTE-Link, an 800x480 landscape e-paper desktop status panel on Xteink X4.
Primary request: Create one polished original compact symbol combining two diagonally interlocking screen-like rectangular links into a subtle X, suggesting the connection between a PC and a small e-paper display. A calm technical instrument identity, visually compatible with Lucide outline icons.
Style: extremely simple geometric black ink, uniform bold monoline strokes with gently rounded corners, pure white negative space. Both link centers are generously open, with clean understandable over/under gaps at their intersection.
Composition: exactly ONE centered mark, square 1024x1024 canvas with the black mark confined within the central 640x640 area, generous even white margins.
Small-size behavior: must remain distinct at 32x32 and 24x24; only two large linked shapes, no additional tiny details. Favor clear silhouette and larger crossing gaps over clever intricacy.
Color: black #000000 on solid white #ffffff, no gray fills, no texture, no lighting, no shadows, no gradients, no mockup.
Text: absolutely NO text, NO letters, NO captions, NO wordmark.
Avoid: thin strokes, chain illustrations with extra links, generic wifi symbol, lightning symbol, circuits, mascots, decorative dots, multiple alternatives, borders around the canvas.

## Flat grayscale refinement (selected)

Edit target: initial generated monochrome concept, preserved as the input to the built-in image edit call.

Use case: logo-brand
Asset type: final compact XTE-Link e-paper app logo.
Input image: the provided logo is the edit target. Preserve its two interlocking rounded rectangular screen-link shapes, crossing gaps, silhouette and centered composition.
Primary request: make this symbol match a white-background flat grayscale icon family. Fill the two enclosed screen interiors in a uniform light gray #aaaaaa, keep bold black #000000 outlines and a pure white #ffffff background and white crossing gaps. Gray screen interiors make the two connected panels legible as screens. Keep their interiors completely empty.
Style: flat, precisely geometric, crisp, three solid colors only, no gradient or texture or soft shadow or blur.
Small size: readable at 32 and 24 pixels, no thin details.
Constraints: exactly one centered symbol. No letters, no words, no mockup, no extra elements. Preserve the original link silhouette.

## Deterministic exports

The selected original is preserved in originals/logo-imagegen.png. export.cjs sizes it and quantizes exported assets to the device palette without dithering. PNG and BMP exports share identical pixels. This step changes export dimensions and palette, not the logo design.

