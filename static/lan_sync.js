/**
 * Offline LAN Real-Time Synchronization Engine
 * Operates strictly over Local Area Network (no internet required).
 * Automatically detects changes across multi-terminal setups and triggers silent in-place UI refreshes.
 */
class LanSyncEngine {
    constructor(options = {}) {
        this.currentVersion = 0;
        this.listeners = [];
        this.ws = null;
        this.reconnectTimer = null;
        this.pingTimer = null;
        this.pollingTimer = null;
        this.reconnectDelay = 1500;
        this.maxReconnectDelay = 10000;
        this.consecutiveFailures = 0;
        this.debounceTimeout = null;
        this.badge = null;
        this.options = Object.assign({
            autoBadge: true,
            pollInterval: 8000,
            debounceMs: 250
        }, options);

        this.init();
    }

    init() {
        if (this.options.autoBadge) {
            // Ensure DOM is ready before injecting badge
            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', () => this.createStatusBadge());
            } else {
                this.createStatusBadge();
            }
        }
        this.connect();
        this.startFallbackPolling();

        // Listen for tab focus/visibility change to resync instantly
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') {
                this.checkSyncVersion();
                if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
                    this.connect();
                }
            }
        });
    }

    createStatusBadge() {
        if (document.getElementById('lan-sync-status-badge')) return;
        const badge = document.createElement('div');
        badge.id = 'lan-sync-status-badge';
        badge.innerHTML = `
            <div id="lan-sync-indicator" style="
                position: fixed;
                bottom: 12px;
                right: 12px;
                background: rgba(15, 23, 42, 0.88);
                color: #e2e8f0;
                padding: 4px 10px;
                border-radius: 9999px;
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                font-size: 11px;
                font-weight: 500;
                display: flex;
                align-items: center;
                gap: 6px;
                box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
                border: 1px solid rgba(255, 255, 255, 0.1);
                z-index: 99999;
                pointer-events: auto;
                cursor: default;
                backdrop-filter: blur(4px);
                transition: all 0.3s ease;
            " title="Offline LAN Real-Time Sync Active">
                <span id="lan-sync-dot" style="
                    width: 7px;
                    height: 7px;
                    border-radius: 50%;
                    background: #10b981;
                    display: inline-block;
                    box-shadow: 0 0 8px #10b981;
                    transition: background 0.3s ease;
                "></span>
                <span id="lan-sync-text">LAN Synced</span>
            </div>
        `;
        document.body.appendChild(badge);
        this.badge = badge;
    }

    updateBadge(status, text) {
        const dot = document.getElementById('lan-sync-dot');
        const label = document.getElementById('lan-sync-text');
        const container = document.getElementById('lan-sync-indicator');
        if (!dot || !label) return;

        if (status === 'connected') {
            dot.style.background = '#10b981';
            dot.style.boxShadow = '0 0 8px #10b981';
            label.textContent = text || `LAN Synced`;
            if (container) container.title = `Connected to LAN Server. Active Data Version: ${this.currentVersion}`;
        } else if (status === 'syncing') {
            dot.style.background = '#3b82f6';
            dot.style.boxShadow = '0 0 8px #3b82f6';
            label.textContent = text || 'Syncing...';
        } else {
            dot.style.background = '#f59e0b';
            dot.style.boxShadow = '0 0 8px #f59e0b';
            label.textContent = text || 'LAN Polling';
            if (container) container.title = 'WebSocket reconnecting; HTTP polling active.';
        }
    }

    connect() {
        if (this.ws && (this.ws.readyState === WebSocket.CONNECTING || this.ws.readyState === WebSocket.OPEN)) {
            return;
        }

        try {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${protocol}//${window.location.host}/ws/updates`;
            this.ws = new WebSocket(wsUrl);

            this.ws.onopen = () => {
                this.consecutiveFailures = 0;
                this.reconnectDelay = 1500;
                this.updateBadge('connected');
                this.startHeartbeat();
            };

            this.ws.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    if (data.event === 'CONNECTED') {
                        this.currentVersion = data.version || 1;
                        this.updateBadge('connected');
                        return;
                    }
                    if (data.version && data.version > this.currentVersion) {
                        this.currentVersion = data.version;
                        this.dispatchUpdate(data);
                    } else if (data.event) {
                        this.dispatchUpdate(data);
                    }
                } catch (err) {
                    console.debug('LAN Sync message parse error:', err);
                }
            };

            this.ws.onclose = () => {
                this.stopHeartbeat();
                this.consecutiveFailures++;
                if (this.consecutiveFailures >= 3) {
                    this.updateBadge('polling', 'LAN Polling (HTTP)');
                } else {
                    this.updateBadge('disconnected');
                }
                this.scheduleReconnect();
            };

            this.ws.onerror = () => {
                try { this.ws.close(); } catch(e) {}
            };
        } catch (err) {
            this.consecutiveFailures++;
            this.updateBadge('disconnected');
            this.scheduleReconnect();
        }
    }

    startHeartbeat() {
        this.stopHeartbeat();
        this.pingTimer = setInterval(() => {
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                this.ws.send('ping');
            }
        }, 20000);
    }

    stopHeartbeat() {
        if (this.pingTimer) {
            clearInterval(this.pingTimer);
            this.pingTimer = null;
        }
    }

    scheduleReconnect() {
        if (this.reconnectTimer) return;
        const delay = this.consecutiveFailures >= 3 ? 30000 : this.reconnectDelay;
        this.reconnectTimer = setTimeout(() => {
            this.reconnectTimer = null;
            this.reconnectDelay = Math.min(this.reconnectDelay * 1.5, this.maxReconnectDelay);
            this.connect();
        }, delay);
    }

    startFallbackPolling() {
        if (this.pollingTimer) clearInterval(this.pollingTimer);
        this.pollingTimer = setInterval(() => {
            this.checkSyncVersion();
        }, this.options.pollInterval);
    }

    async checkSyncVersion() {
        try {
            const res = await fetch('/api/sync-version', { cache: 'no-store' });
            if (!res.ok) return;
            const data = await res.json();
            if (data && data.version) {
                if (this.currentVersion === 0) {
                    this.currentVersion = data.version;
                } else if (data.version > this.currentVersion) {
                    this.currentVersion = data.version;
                    this.dispatchUpdate({
                        event: 'VERSION_DESYNC_DETECTED',
                        version: data.version,
                        details: { source: 'polling' }
                    });
                }
            }
        } catch (err) {
            // Offline or server temporarily busy
        }
    }

    dispatchUpdate(payload) {
        this.updateBadge('syncing', 'Syncing...');
        clearTimeout(this.debounceTimeout);
        this.debounceTimeout = setTimeout(() => {
            // Trigger registered callback functions
            this.listeners.forEach((fn) => {
                try {
                    fn(payload);
                } catch (e) {
                    console.error('LAN Sync subscriber error:', e);
                }
            });

            // Dispatch global CustomEvent on window
            window.dispatchEvent(new CustomEvent('lan-data-updated', { detail: payload }));

            setTimeout(() => {
                this.updateBadge('connected');
            }, 600);
        }, this.options.debounceMs);
    }

    onUpdate(callback) {
        if (typeof callback === 'function') {
            this.listeners.push(callback);
        }
    }
}

// Global Singleton Instance
window.lanSync = new LanSyncEngine();
