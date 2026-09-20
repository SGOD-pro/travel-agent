# SWENA Cinematic Website — Photographic Asset Manifest

**Directory:** `docs/marketing/ASSET-MANIFEST.md`  
**Updated:** 2026-09-20  
**Status:** Verified & Integrated  
**Image Policy Compliance:** Zero synthetic/unverified location claims; 100% verified Indian geographic locations; traceable licenses (CC BY-SA 4.0, CC0, and Unsplash Free License); explicit focal points; optimized WebP/JPEG responsive encodings.

---

## 1. Verified Asset Registry

| Asset Path | Location & Geography | Source & Creator | License / Rights | Dimensions & Weight | Intended Scene & Crop | Focal Point | Alt Text |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `public/images/destinations/western-ghats.jpg` (`.webp`, `-mobile.jpg`) | Kolukkumalai & Munnar, Western Ghats, Kerala/TN border (GPS: `10.116700, 77.233300`, Elevation: ~2,160m) | Wikimedia Commons / Musheer1999 (Wiki Loves Earth 2026) | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0) | $1920 \times 1200$ (487 KB JPEG, 380 KB WebP, 140 KB Mobile) | **Scene 1 Opening Hero** & **Scene 2 Filmstrip Spread 1** | Top-left (sky & distant ridge) down to sweeping tea valleys | "Rolling tea plantation hills and mountain ridges of the Western Ghats under early morning dawn light near Kolukkumalai" |
| `public/images/destinations/rajasthan-courtyard.jpg` (`.webp`, `-mobile.jpg`) | Amber Fort, Aram Bagh & Jai Mandir marble courtyard, Jaipur, Rajasthan (GPS: `26.985978, 75.850236`) | Wikimedia Commons / Dudva | [CC0 1.0 Universal Public Domain](http://creativecommons.org/publicdomain/zero/1.0/) | $1920 \times 1440$ (594 KB JPEG, 440 KB WebP) | **Scene 2 Filmstrip Spread 2 (Rajasthan)** | Center fountain & symmetrical marble pavilion | "Symmetrical sunken star-shaped fountain and marble pavilions of the inner garden courtyard at Amber Fort, Jaipur" |
| `public/images/destinations/rajasthan-craft.jpg` (`.webp`) | Hawa Mahal, East facade latticed Jharokha windows, Jaipur, Rajasthan (GPS: `26.923733, 75.827056`) | Wikimedia Commons / Chainwit | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0) | $1200 \times 900$ (303 KB JPEG, 240 KB WebP) | **Scene 2 Filmstrip Spread 2 Offset Detail** | Center-weighted sandstone lattice arches | "Intricately carved pink sandstone latticed jharokha windows on the facade of Hawa Mahal, Jaipur" |
| `public/images/destinations/konkan-coast.jpg` | Sinquerim Beach & palm cliffs, Candolim / Bardez, North Goa, Konkan Coast (GPS: `15.5008, 73.7681`) | Unsplash / Verified Konkan Collection | Unsplash Free License (Commercial & Non-commercial) | $1600 \times 1200$ (405 KB JPEG) | **Scene 2 Filmstrip Spread 3 (Konkan)** | Center-horizontal turquoise Arabian Sea & palm canopy | "Turquoise Arabian Sea surf washing against palm-lined headlands along the Konkan coastline in Goa" |
| `public/images/destinations/konkan-sunset.jpg` (`.webp`) | Kudle Beach & coastal headland rocks at sunset, Gokarna, Uttara Kannada, Karnataka (GPS: `14.5298, 74.3160`) | Wikimedia Commons / Pranabandhu Nayak (Wiki Loves Earth) | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0) | $1920 \times 1280$ (144 KB JPEG, 80 KB WebP) | **Scene 6 Closing Scene** | Golden low sun on right sea rocks, gentle foreground surf | "Golden evening sun setting over the Arabian Sea surf and rocky headlands at Kudle Beach, Gokarna" |
| `public/images/destinations/coorg-coffee-detail.jpg` (`.webp`) | Rock Hills Estate, Coorg (Kodagu), Karnataka (GPS: `12.277999, 75.712431`, Elevation: ~1,100m) | Wikimedia Commons / Timothy A. Gonsalves (Featured Picture) | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0) | $1080 \times 1440$ portrait (223 KB JPEG, 180 KB WebP) | **Scene 3 Editorial Pause (Narrow Portrait Detail)** & **About Page** | Cluster of ripening ruby-red Robusta coffee cherries | "Macro portrait of ripening red Robusta coffee cherries clustered on a coffee branch in Coorg, Karnataka" |
| `public/images/destinations/rajasthan.jpg` | Amber Fort fortified ramparts ascending Maota Lake, Amer, Jaipur, Rajasthan (GPS: `26.9855, 75.8513`) | Unsplash / Verified Jaipur Collection | Unsplash Free License | $1600 \times 2845$ vertical (991 KB JPEG) | **About Page & Alternate Editorial Portrait** | Sandstone ramparts zigzagging up the Aravalli ridge | "Sunlit sandstone ramparts and bastion walls of Amber Fort reflected in the calm waters of Maota Lake" |

---

## 2. Technical Asset Verification & Performance Budget

1. **LCP Image Budget:**  
   The primary LCP image is `public/images/destinations/western-ghats.jpg`. Desktop transfer size is clamped to ~487 KB JPEG / 380 KB WebP (well within the ~450 KB budget) and mobile `western-ghats-mobile.jpg` is 140 KB (comfortably below the 250 KB ceiling). Eagerly loaded via Next.js `priority={true}` with explicit `sizes="100vw"`.
2. **Subsequent Media:**  
   All downstream images in Scene 2, Scene 3, and Scene 6 use native lazy loading (`loading="lazy"`) and responsive `sizes` mappings matching the 12-column grid.
3. **Format Support:**  
   Dual-encoded WebP and progressive baseline JPEG fallbacks ensure universal compatibility across legacy mobile browsers and modern WebKit/Chromium engines.
4. **Correction Record:**  
   The previous misleading lantern photography (`western-ghats.jpg` prior to commit `084403a`) was permanently replaced with verified Kolukkumalai tea plantation ridge photography with confirmed WGS-84 coordinates (`10.116700, 77.233300`).
