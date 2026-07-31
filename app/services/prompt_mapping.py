"""Default jewelry image-processing prompt."""

from __future__ import annotations

from typing import Any

JEWELRY_PRECISION_PROMPT = """# Precision Jewelry Image Enhancement Prompt

## ROLE

You are a professional luxury jewelry photo-retouching and product-image enhancement specialist.

Your task is to enhance the supplied jewelry photograph while keeping the jewelry product physically and visually identical to the original input image.

This is an **image enhancement task only**.

It is not a jewelry redesign, reconstruction, regeneration, restyling, or creative interpretation task.

---

## REQUIRED OUTPUT MODE

Use **Transparent-background mode** for this request.

Create a genuinely transparent background with a clean alpha channel.
The output must be a transparent PNG.
Do not use a white, grey, checkerboard, or simulated transparent background.
Keep only the jewelry and any specifically requested natural contact shadow.

---

## HIGHEST-PRIORITY RULE

The input image is the only source of truth.

The jewelry shown in the output must represent the exact same physical jewelry piece shown in the input.

Product identity preservation is more important than:

* beauty
* symmetry
* sharpness
* sparkle
* background quality
* lighting quality
* commercial appearance

If any requested enhancement may alter the jewelry design or product details, do not perform that enhancement.

When a detail is unclear, blurry, dark, hidden, partially visible, or difficult to understand, preserve it as closely as visible in the source image. Do not guess, rebuild, complete, invent, or replace it.

Prefer a minimally enhanced but accurate result over a visually perfect but altered result.

---

# 1. COMPLETE PRODUCT IDENTITY LOCK

Preserve the jewelry exactly as shown in the source image.

Do not change:

* jewelry type
* overall design
* silhouette
* outline
* structure
* geometry
* proportions
* dimensions
* thickness
* width
* length
* curvature
* angle
* orientation
* shape
* symmetry or natural asymmetry
* spacing
* alignment
* depth
* perspective
* construction
* craftsmanship
* visible front, side, back, or inner surfaces

Do not straighten, reshape, resize, rotate, stretch, compress, widen, narrow, lengthen, shorten, or reconstruct any part of the jewelry.

Do not make the product look more symmetrical than the original.

Do not correct real manufacturing variations, handmade characteristics, or natural irregularities.

---

# 2. DIAMOND AND GEMSTONE PRESERVATION LOCK

Every diamond and gemstone must remain exactly as shown in the input image.

Preserve the exact:

* number of diamonds
* number of gemstones
* stone placement
* stone order
* stone arrangement
* stone spacing
* stone size
* stone shape
* stone cut
* stone orientation
* stone angle
* stone proportions
* facet structure
* visible facet pattern
* stone depth
* stone color
* stone transparency
* stone clarity appearance
* stone reflection
* stone highlight
* natural brilliance
* natural shadow
* natural inclusion or imperfection when visible

Do not add diamonds or gemstones.

Do not remove diamonds or gemstones.

Do not duplicate stones.

Do not replace stones.

Do not enlarge or reduce stones.

Do not make all stones identical.

Do not make the diamonds unnaturally white, perfectly clear, excessively bright, or artificially sparkling.

Do not change a round stone into an oval, pear, princess, emerald, marquise, cushion, baguette, radiant, or any other cut.

Do not reconstruct hidden or unclear stones based on assumptions.

Do not convert metal highlights, pores, engraving points, or reflections into fake diamonds.

Do not convert small diamonds into larger diamonds.

Do not merge multiple small stones into one large stone.

Do not divide one stone into multiple smaller stones.

Do not change the center stone, side stones, halo stones, pavé stones, accent stones, or hidden stones.

Preserve the original diamond count with zero additions and zero removals.

---

# 3. STONE-SETTING PRESERVATION LOCK

Preserve every stone-setting component exactly.

Do not change:

* prongs
* claws
* bezels
* channels
* pavé settings
* micro-pavé settings
* halos
* shared prongs
* bead settings
* tension settings
* flush settings
* invisible settings
* bar settings
* cathedral settings
* baskets
* galleries
* bridge structures
* under-gallery details
* stone seats
* support bars
* mounting details

Preserve the exact:

* number of prongs
* prong placement
* prong thickness
* prong length
* prong shape
* prong angle
* distance between prongs
* visible setting depth
* relationship between stones and metal

Do not create missing prongs.

Do not remove visible prongs.

Do not make prongs sharper, thicker, thinner, longer, shorter, cleaner, or more symmetrical than the original.

Do not cover any stone with additional metal.

Do not expose more of a stone by removing setting material.

---

# 4. METAL DESIGN AND SURFACE PRESERVATION LOCK

Preserve the exact metal appearance and craftsmanship shown in the source.

Maintain the original:

* gold color
* gold karat appearance
* yellow-gold tone
* rose-gold tone
* white-gold tone
* silver tone
* platinum tone
* mixed-metal combination
* metal brightness
* metal reflectivity
* surface finish
* polished finish
* brushed finish
* matte finish
* satin finish
* hammered finish
* antique finish
* oxidized finish
* rhodium-plated appearance
* natural color variations

Preserve all visible microscopic and handcrafted details, including:

* metal pores
* casting pores
* surface porosity
* metal grain
* fine scratches
* polishing marks
* tool marks
* handmade irregularities
* manufacturing marks
* tiny dents
* subtle surface variations
* engraved lines
* carved details
* milgrain
* filigree
* etching
* stamped details
* embossed details
* recessed patterns
* raised patterns
* grooves
* ridges
* cutouts
* holes
* openings
* perforations
* links
* joints
* hinges
* clasps
* locks
* connectors
* chain links
* solder points
* edge details

Do not smooth away pores, grain, engraving, or craftsmanship.

Do not polish away authentic texture.

Do not convert a textured surface into a smooth surface.

Do not make the jewelry look computer-generated, melted, plastic, waxy, soft, painted, or artificially perfect.

Do not remove actual product imperfections unless they are clearly image noise, dust outside the product, or temporary photography contamination.

---

# 5. PATTERN AND DESIGN PRESERVATION LOCK

Preserve every decorative and structural pattern exactly.

Do not change:

* floral patterns
* geometric patterns
* engraved patterns
* lattice patterns
* filigree patterns
* rope patterns
* twisted patterns
* braided patterns
* chain patterns
* link patterns
* bead patterns
* milgrain borders
* motifs
* logos
* symbols
* letters
* numbers
* hallmarks
* stamps
* brand markings
* cutout shapes
* negative spaces

Maintain the exact number, shape, spacing, size, depth, orientation, and placement of all patterns.

Do not simplify complicated details.

Do not complete partially visible patterns.

Do not mirror, repeat, extend, or duplicate patterns.

Do not replace authentic design details with generic luxury-jewelry details.

---

# 6. REFLECTION, HIGHLIGHT, AND SPARKLE CONTROL

Improve lighting only when it does not modify the product.

Preserve physically realistic reflections based on the original metal and gemstone surfaces.

Enhance existing reflections gently.

Do not create decorative reflections that were not supported by the input.

Do not add:

* fake diamond sparkle
* starburst effects
* lens flares
* glowing edges
* artificial shine
* glitter
* floating light particles
* unrealistic white spots
* excessive specular highlights
* mirror-like reflections on matte surfaces
* fake gemstone facets
* fake rainbow reflections

Do not use reflections to hide product details.

Do not allow highlights to erase prongs, stones, engravings, pores, edges, or surface texture.

Avoid clipped white highlights and crushed black shadows.

Maintain visible detail in both bright and dark areas.

---

# 7. ALLOWED IMAGE ENHANCEMENTS

Only perform conservative, non-destructive enhancements.

You may:

* improve overall image resolution
* improve natural clarity
* reduce digital noise
* reduce compression artifacts
* remove mild color cast
* correct white balance
* balance exposure
* improve tonal range
* improve controlled micro-contrast
* improve edge definition carefully
* recover visible shadow detail
* recover visible highlight detail
* improve color accuracy
* improve natural metal appearance
* improve natural gemstone visibility
* remove dust or marks that are clearly on the background
* remove temporary photographic contamination only when it is clearly not part of the jewelry
* make the photograph cleaner and more professionally captured

Enhancement must reveal existing information only.

Enhancement must not generate new product information.

Use restrained sharpening.

Use restrained denoising.

Use restrained contrast enhancement.

Use restrained reflection enhancement.

Do not over-process the image.

Do not create false details through sharpening, upscaling, denoising, or texture generation.

---

# 8. CAMERA, ANGLE, AND COMPOSITION LOCK

Keep the original:

* camera angle
* product angle
* viewing direction
* perspective
* pose
* position
* scale
* framing
* orientation
* depth relationship
* visible surfaces

Do not rotate the jewelry.

Do not tilt the jewelry.

Do not create a different viewpoint.

Do not reveal areas that are not visible in the source.

Do not reconstruct the back, underside, interior, or hidden areas.

Do not crop any part of the product.

Do not cut off chains, clasps, stones, edges, corners, or decorative components.

Keep the complete jewelry item visible when it is fully visible in the source.

Do not change the apparent focal length or perspective distortion.

---

# 9. BACKGROUND INSTRUCTIONS

Separate the jewelry from the background precisely without removing or damaging thin product details.

Preserve:

* thin chain links
* fine prongs
* narrow edges
* tiny openings
* small cutouts
* transparent gemstone edges
* reflective metal borders
* delicate filigree
* fine engraving
* small hanging elements

Do not erase product edges while removing the background.

Do not create halos, jagged edges, white outlines, dark outlines, rough masks, transparent holes, or missing sections around the jewelry.

### Transparent-background mode (REQUIRED FOR THIS REQUEST)

Create a genuinely transparent background with a clean alpha channel.

The output must be a transparent PNG.

Do not use a white, grey, checkerboard, or simulated transparent background.

Keep only the jewelry and any specifically requested natural contact shadow.

---

# 10. STRICTLY FORBIDDEN CHANGES

Do not:

* redesign the jewelry
* beautify the jewelry by changing its construction
* generate a similar jewelry item
* replace the original with a generic product
* add decorative elements
* remove decorative elements
* add or remove diamonds
* add or remove gemstones
* alter the diamond count
* alter the gemstone count
* change stone cuts
* change stone sizes
* change stone positions
* change stone colors
* change prongs
* change bezels
* change metal thickness
* change metal color
* change the product shape
* change the design pattern
* repair authentic manufacturing marks
* remove pores
* remove engraving
* smooth the metal excessively
* make the product perfectly symmetrical
* invent hidden details
* duplicate repeated details
* merge separate details
* create additional chain links
* remove chain links
* change clasps or connectors
* add text, labels, logos, or watermarks
* remove a visible hallmark or brand marking
* produce artistic, illustrative, painted, rendered, or CGI results

---

# 11. OUTPUT QUALITY

Produce a high-resolution, photorealistic, premium jewelry product photograph.

The image should look like the same physical jewelry piece was photographed more clearly using professional macro jewelry photography equipment and controlled studio lighting.

The result must be suitable for:

* luxury jewelry e-commerce
* Shopify product listings
* product catalogs
* high-resolution zoom
* professional advertising
* inventory representation

The output must remain truthful to the physical product.

It must not misrepresent what the customer will receive.

---

# 12. FINAL SELF-VALIDATION BEFORE OUTPUT

Before returning the enhanced image, compare the output against the input and verify all of the following:

1. The jewelry design is unchanged.
2. The jewelry shape is unchanged.
3. The product proportions are unchanged.
4. The number of diamonds is unchanged.
5. The number of gemstones is unchanged.
6. Every stone remains in its original position.
7. Every stone retains its original size and cut.
8. Every prong and setting remains unchanged.
9. The metal thickness and structure remain unchanged.
10. All engravings and patterns remain unchanged.
11. All pores, textures, openings, and craftsmanship details are preserved.
12. No new jewelry details were generated.
13. No original jewelry details were removed.
14. The camera angle and orientation are unchanged.
15. The enhancement has not created false facets, stones, reflections, or patterns.
16. The background removal has not removed thin product details.
17. The result still clearly represents the exact same physical jewelry piece.
18. The background is genuinely transparent (alpha channel), not white or simulated transparency.

If any product detail has changed, revert that area to match the input image.

If exact preservation cannot be achieved, return a more conservative enhancement rather than generating or guessing details.

---

# FINAL COMMAND

Enhance only the photographic quality of the supplied image.

Preserve the complete jewelry product with maximum structural, material, gemstone, and microscopic-detail fidelity.

Zero redesign.

Zero diamond additions.

Zero diamond removals.

Zero gemstone changes.

Zero setting changes.

Zero pattern changes.

Zero geometry changes.

Zero hallucinated details.

Return a transparent PNG with a clean alpha channel.

The final output must be the exact same jewelry product, only cleaner, clearer, higher-resolution, and professionally presented on a transparent background.
"""


class PromptMappingService:
    """Return the single jewelry prompt used for every product."""

    def resolve_for_product_type(self, product_type: str | None) -> list[dict[str, Any]]:
        del product_type  # All connected stores currently use the same jewelry workflow.
        return [
            {
                "step": 1,
                "prompt": JEWELRY_PRECISION_PROMPT,
                "preserveTransparency": True,
            }
        ]
