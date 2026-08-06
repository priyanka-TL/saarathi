import { InfoIcon } from '../icons';
import BrandLogo from './BrandLogo';

/**
 * The header card. Static.
 *
 * Note `.brand-text h1` asks for 'Public Sans' in style.css, which is NOT in
 * the Google Fonts link -- so the wordmark renders in the fallback. That is
 * shipped behaviour; adding the font would change its metrics.
 */
export default function BrandCard() {
  return (
    <div className="brand-card">
      <div className="brand-left">
        <div className="brand-icon">
          <BrandLogo />
        </div>
        <div className="brand-text">
          <h1>Saarthi</h1>
          <p>Helping You Every Step of the Way</p>
        </div>
      </div>
      <div className="brand-right">
        <div className="status-row">
          <span className="status-dot" />
          Online
        </div>
        <div className="meta-row">
          <InfoIcon />
          DPI-aligned
        </div>
      </div>
    </div>
  );
}
