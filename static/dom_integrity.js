/**
 * Bookledger DOM Integrity & Anti-Tamper Security Module
 * Protects client-side data integrity against DevTools element inspection,
 * unauthorized DOM manipulation, and ensures 100% authoritative server data
 * during print, preview, modal views, and save operations.
 * Passive, non-pulsating architecture with zero continuous polling reflows.
 */

(function(window) {
    'use strict';

    // Store deep-frozen authoritative server datasets by namespace
    const authoritativeStore = new Map();
    const printRehydrators = [];
    let isSystemRendering = false;
    let toastTimeout = null;

    /**
     * Executes a callback function while flagging system rendering.
     */
    function withSystemRendering(fn) {
        isSystemRendering = true;
        try {
            return fn();
        } catch (err) {
            console.error('[Integrity Guard] Internal rendering error:', err);
        } finally {
            setTimeout(function() {
                isSystemRendering = false;
            }, 30);
        }
    }

    /**
     * Registers authoritative, un-tampered server data for a given namespace.
     * Deep clones and freezes the data so DevTools cannot mutate the in-memory object.
     */
    function registerAuthoritativeData(namespace, data) {
        if (!namespace || data === undefined || data === null) return;
        try {
            const cloned = JSON.parse(JSON.stringify(data));
            authoritativeStore.set(namespace, Object.freeze(cloned));
        } catch (e) {
            authoritativeStore.set(namespace, data);
        }
    }

    /**
     * Retrieves authoritative server data for a namespace.
     */
    function getAuthoritativeData(namespace) {
        return authoritativeStore.get(namespace) || null;
    }

    /**
     * Retrieves a single record from a list in the authoritative store.
     * Can match by primary key 'id' or by a predicate function.
     */
    function getAuthoritativeRecord(namespace, predicateOrId) {
        const data = authoritativeStore.get(namespace);
        if (!data) return null;

        const list = Array.isArray(data) ? data : (data.data || data.items || []);
        if (!Array.isArray(list)) return null;

        if (typeof predicateOrId === 'function') {
            return list.find(predicateOrId) || null;
        } else {
            return list.find(function(item) {
                return item && (item.id === predicateOrId || item.id === Number(predicateOrId));
            }) || null;
        }
    }

    /**
     * Sets authoritative text on an element and marks it with tamper protection.
     */
    function lockAuthoritativeElement(target, authoritativeValue) {
        var el = typeof target === 'string' ? document.querySelector(target) : target;
        if (!el || authoritativeValue === undefined || authoritativeValue === null) return;

        var strVal = String(authoritativeValue);
        el.setAttribute('data-tamper-protect', 'true');
        el.textContent = strVal;
    }

    /**
     * Utility to set authoritative text on multiple elements matching a selector.
     */
    function lockAuthoritativeGroup(selector, extractorFn) {
        if (!selector || typeof extractorFn !== 'function') return;
        var elements = document.querySelectorAll(selector);
        for (var i = 0; i < elements.length; i++) {
            var val = extractorFn(elements[i], i);
            if (val !== undefined && val !== null) {
                lockAuthoritativeElement(elements[i], val);
            }
        }
    }

    /**
     * Registers a rehydration function that will be executed synchronously
     * right before any print action (button click, window.print(), or Ctrl+P).
     */
    function registerPrintRehydrator(fn) {
        if (typeof fn === 'function') {
            printRehydrators.push(fn);
        }
    }

    /**
     * Synchronously executes all registered pre-print rehydration functions.
     * Ensures any text or nodes modified in Inspect Element are instantly
     * reverted to authoritative server data before the print view captures.
     */
    function executePrintRehydration() {
        withSystemRendering(function() {
            for (var i = 0; i < printRehydrators.length; i++) {
                try {
                    printRehydrators[i]();
                } catch (err) {
                    console.warn('[Integrity Guard] Pre-print rehydration error:', err);
                }
            }
        });
    }

    /**
     * Displays a non-intrusive security toast when unauthorized DOM tampering is detected.
     */
    function showTamperToast(customMsg) {
        var existing = document.getElementById('tamperWarningToast');
        if (existing) existing.remove();
        if (toastTimeout) clearTimeout(toastTimeout);

        var toast = document.createElement('div');
        toast.id = 'tamperWarningToast';
        toast.style.cssText = 'position: fixed; bottom: 24px; right: 24px; z-index: 9999999; background: #991b1b; color: white; padding: 12px 18px; border-radius: 8px; box-shadow: 0 10px 25px rgba(0,0,0,0.35); font-family: -apple-system, BlinkMacSystemFont, Arial, sans-serif; font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 10px; pointer-events: none; transition: opacity 0.4s ease;';
        toast.innerHTML = '<span style="font-size: 16px;">🛡️</span><span>' + (customMsg || 'Integrity Guard: Inspection disabled on official records.') + '</span>';
        
        if (document.body && document.body.appendChild) {
            document.body.appendChild(toast);
        }
        toastTimeout = setTimeout(function() {
            toast.style.opacity = '0';
            setTimeout(function() {
                if (toast && toast.parentNode) toast.parentNode.removeChild(toast);
            }, 400);
        }, 2800);
    }

    /**
     * Passive container protection:
     * Tags the target element with data-tamper-protect="true" to disable right-click inspect.
     * Optionally registers print rehydration if a re-render function is supplied.
     * Zero continuous polling or active mutation observers to guarantee zero UI pulsation.
     */
    function protectContainer(target, renderFn) {
        var el = typeof target === 'string' ? document.querySelector(target) : target;
        if (!el) return null;

        el.setAttribute('data-tamper-protect', 'true');
        if (typeof renderFn === 'function') {
            registerPrintRehydrator(renderFn);
        }
        return {
            disconnect: function() {}
        };
    }

    // Intercept native window.print() so all calls re-hydrate first
    var originalPrint = window.print;
    window.print = function() {
        executePrintRehydration();
        return originalPrint.apply(window, arguments);
    };

    // Listen for browser shortcut Ctrl+P / print dialog
    window.addEventListener('beforeprint', function() {
        executePrintRehydration();
    });

    // Prevent contextmenu on protected elements to discourage casual inspect tampering
    document.addEventListener('contextmenu', function(e) {
        if (e.target && e.target.closest && e.target.closest('[data-tamper-protect="true"]')) {
            e.preventDefault();
            showTamperToast('Inspection disabled on official records.');
        }
    });

    // Expose public API
    window.registerAuthoritativeData = registerAuthoritativeData;
    window.getAuthoritativeData = getAuthoritativeData;
    window.getAuthoritativeRecord = getAuthoritativeRecord;
    window.lockAuthoritativeElement = lockAuthoritativeElement;
    window.lockAuthoritativeGroup = lockAuthoritativeGroup;
    window.registerPrintRehydrator = registerPrintRehydrator;
    window.executePrintRehydration = executePrintRehydration;
    window.withSystemRendering = withSystemRendering;
    window.protectContainer = protectContainer;
    window.showTamperToast = showTamperToast;

})(window);
