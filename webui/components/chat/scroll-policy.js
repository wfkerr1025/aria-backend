/* webui/components/chat/scroll-policy.js
 *
 * Whether the transcript should scroll, decided without touching the DOM.
 *
 * chat.js used to run `history.scrollTop = history.scrollHeight` on every
 * stream_token. That is fine while you are at the bottom watching the
 * answer arrive, and hostile the moment you are not: scrolling up to
 * re-read something during a long answer meant being yanked back down
 * roughly thirty times a second, with no way to win.
 *
 * The rule this implements is the one every chat client converges on:
 * follow the output only for as long as the reader is actually at the
 * bottom. Scrolling up is the reader taking over, and it holds until they
 * come back down of their own accord.
 *
 * It is a pure function of three numbers so it can be tested without a
 * browser -- see scroll-policy.test.js. chat.js reads the numbers off the
 * element and does what this returns.
 */

// How far from the bottom still counts as "at the bottom".
//
// Not zero: scrollHeight and clientHeight are fractional at non-integer
// zoom levels and after a font swap, so an exact comparison reports a
// reader who has not moved as having scrolled away. A couple of lines of
// slack also matches what the eye calls the bottom.
const BOTTOM_THRESHOLD_PX = 48;

function isAtBottom(metrics, threshold = BOTTOM_THRESHOLD_PX) {
  const { scrollTop = 0, scrollHeight = 0, clientHeight = 0 } = metrics || {};
  return scrollHeight - (scrollTop + clientHeight) <= threshold;
}

/*
 * shouldFollow -- should the view scroll to the bottom right now?
 *
 *   metrics  { scrollTop, scrollHeight, clientHeight }
 *   state    { pinned }  whether the reader is currently following along
 *   reason   "token" | "stream_end" | "new_message"
 *
 * Returns { follow, pinned }: whether to scroll now, and the pinned flag
 * to carry into the next call.
 *
 * `pinned` is recomputed from the live position on every token rather
 * than latched once at stream_start, so a reader who scrolls up mid-answer
 * is released immediately and one who scrolls back down is picked up again
 * without having to wait for the next turn.
 */
function shouldFollow(metrics, state, reason) {
  const atBottom = isAtBottom(metrics);

  // The reader sending a message is the one unambiguous request to be at
  // the bottom: they just acted, and their own words are down there.
  if (reason === "new_message") {
    return { follow: true, pinned: true };
  }

  if (reason === "stream_end") {
    // A single catch-up at the end, and only for a reader who never left.
    // "Optionally scroll once, but never yank the user away from where
    // they are reading" -- so being away is what decides it, not the fact
    // that a turn finished.
    return { follow: atBottom, pinned: atBottom };
  }

  // reason === "token", the common case.
  return { follow: atBottom, pinned: atBottom };
}

/* Node (tests) and the browser both, without a build step. */
if (typeof module !== "undefined" && module.exports) {
  module.exports = { shouldFollow, isAtBottom, BOTTOM_THRESHOLD_PX };
}
if (typeof window !== "undefined") {
  window.AriaScrollPolicy = { shouldFollow, isAtBottom, BOTTOM_THRESHOLD_PX };
}
