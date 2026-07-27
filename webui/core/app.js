// core/app.js
import { bridge } from './bridge.js';
import { Router } from './router.js';
import Sidebar from '../components/sidebar/sidebar.js';
import { Theme } from './theme-loader.js';

document.addEventListener('DOMContentLoaded', () => {
    console.log("[App] Initializing ARIA Lite...");

    Sidebar.init();
    Router.init(bridge);

    console.log("[App] Connecting WebSocket...");
    bridge.connect();

    console.log("[App] Theme system ready:", Theme);
});
