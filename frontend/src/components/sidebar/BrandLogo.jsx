import logoUrl from '../../assets/saarthi-logo.png';

/**
 * The Saarthi brand mark.
 *
 * THE ONE GRAPHIC THAT IS NOT AN INLINE SVG. Every icon in components/icons is
 * inline because `stroke="currentColor"` picks up CSS variables and an
 * `<img src="*.png">` cannot -- that is why src/assets/ was empty. None of it
 * applies here: this is a fixed-palette brand mark, it must NOT recolour with
 * the theme, and keeping it a standalone file is what lets new artwork be
 * dropped in by overwriting one file.
 *
 * The source is 128x114 -- NOT square, which is why .brand-logo uses
 * `object-fit: contain` rather than stretching to the 46px slot. It is also
 * only just past 2x for that slot, so if the mark ever renders larger than the
 * brand card, it wants re-exporting (ideally as SVG) rather than scaling up.
 *
 * Vite resolves the import to a URL and copies the file into dist/ on build.
 */
export default function BrandLogo() {
  return <img className="brand-logo" src={logoUrl} alt="Saarthi" />;
}
