import { Component } from 'react';

/**
 * Last-resort boundary.
 *
 * The original had no equivalent -- a thrown error simply left a half-rendered
 * page. This renders the app's own system-message styling instead of a blank
 * screen, and is the only place a class component appears (React still has no
 * hook equivalent for componentDidCatch).
 */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    console.error('Unhandled UI error:', error, info);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="app-container">
          <div className="chat-container">
            <div className="chat-inner-box">
              <div className="chat-window-card">
                <main className="chat-messages">
                  <div className="message system">
                    <div className="message-body">
                      <div className="message-content error-content">
                        Something went wrong. Please reload the page.
                      </div>
                    </div>
                  </div>
                </main>
              </div>
            </div>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
