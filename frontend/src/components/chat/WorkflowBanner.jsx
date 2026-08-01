import { Fragment, useState } from 'react';

import { COPY } from '../../constants';
import { cx } from '../../utils/cx';
import { BreadcrumbSeparatorIcon, ChevronDownIcon, WorkflowIcon } from '../icons';

/**
 * The workflow / context banner above the chat card.
 *
 * Two things to preserve exactly:
 *
 * 1. The collapse uses CSS grid `1fr -> 0fr` on .collapsible-content, NOT
 *    max-height (the Advanced panel in the sidebar uses max-height instead --
 *    two different techniques on purpose, with different easing).
 * 2. The `.expanded` class in the original markup has NO CSS rule at all; only
 *    `.collapsed` does anything. It is reproduced here for fidelity but is
 *    inert.
 *
 * The active breadcrumb is the LAST stop (`index === stops.length - 1`), which
 * is what the original used -- deliberately not `currentIndex`, even though
 * the header text is derived from `currentIndex`.
 */
export default function WorkflowBanner({ banner }) {
  const [collapsed, setCollapsed] = useState(false);

  const { hidden, title, stops, currentIndex, label, subLabel } = banner;

  const stopText =
    stops && stops.length > 0 ? `Stop ${(currentIndex || 0) + 1} of ${stops.length}` : '';

  const classes = cx(
    'active-context-banner',
    'collapsible-container',
    collapsed ? 'collapsed' : 'expanded',
    hidden && 'hidden',
  );

  // Matches the original's flat pills: "Home" stays bare, anything else gains
  // " Context"; the sub-context falls back to the label itself.
  const displayLabel = label === COPY.homeContext ? label : `${label} Context`;
  const displaySubLabel = subLabel || label;

  return (
    <div id="active-context-banner" className={classes}>
      <div className="workflow-header-top" onClick={() => setCollapsed((c) => !c)}>
        <div className="workflow-header-title">
          <WorkflowIcon />
          <span id="workflow-title-text" className="workflow-title-text">
            {title || COPY.defaultWorkflowTitle}
          </span>
        </div>
        <div className="workflow-header-actions">
          <span id="workflow-stop-text" className="workflow-stop-text">
            {stopText}
          </span>
          <ChevronDownIcon className="workflow-chevron" />
        </div>
      </div>
      <div className="collapsible-content">
        <div className="collapsible-inner">
          <div id="context-banner-top" className="context-banner-top breadcrumb-nav">
            {stops && stops.length > 0 ? (
              stops.map((stop, index) => {
                const isActive = index === stops.length - 1;
                return (
                  // Fragment, not a wrapper element: the item and its
                  // separator must be direct SIBLINGS inside
                  // #context-banner-top, which is a flex row. Any wrapper
                  // node would become the flex item instead and change the
                  // spacing.
                  <Fragment key={`${stop}-${index}`}>
                    <span className={cx('breadcrumb-item', isActive && 'active')}>
                      {/* Empty node, drawn entirely in CSS: filled for the
                          current stop, hollow for the ones behind it. */}
                      <span className="breadcrumb-dot" />
                      <span className="breadcrumb-label">{stop}</span>
                    </span>
                    {!isActive && (
                      <span className="breadcrumb-separator">
                        <BreadcrumbSeparatorIcon />
                      </span>
                    )}
                  </Fragment>
                );
              })
            ) : (
              <>
                <span className="context-pill primary">
                  {'Context: '}
                  <span id="context-name">{displayLabel}</span>
                </span>
                <span className="context-pill secondary">
                  {'Sub-context: '}
                  <span id="sub-context-name">{displaySubLabel}</span>
                </span>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
