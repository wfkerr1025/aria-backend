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

  if (reason === "assistant_message") {
    // A new answer arriving. Worth positioning for -- the reader wants
    // to see it start -- but only for a reader who is still at the
    // bottom. Someone who scrolled up to re-read something has taken
    // over, and a new message is not a reason to take it back.
    return { follow: atBottom, pinned: atBottom };
  }

  if (reason === "stream_end") {
    // No catch-up. It used to scroll a reader who was still at the
    // bottom down to the end of the finished answer, which is the jump
    // this change exists to remove: they are reading from the top of
    // the message, and arriving at its last line the moment it finishes
    // is exactly the yank being fixed.
    return { follow: false, pinned: atBottom };
  }

  // reason === "token", the common case, and now never a scroll.
  //
  // Following the tail of a long answer means the reader is always
  // looking at its last line, and has to scroll UP to read the thing
  // from the start -- which is what "I have to scroll every message"
  // was describing. It was not that nothing scrolled; it was that the
  // scroll landed in the wrong place.
  //
  // So the view is positioned once, at the top of the new message
  // (chat.js's scrollToMessageTop), and then left alone while the text
  // arrives underneath. That is what Claude and Copilot do, and it is
  // the only arrangement where a long answer is readable as it is
  // written.
  //
  // pinned is still tracked, because stream_end and the next message
  // need to know whether the reader stayed.
  return { follow: false, pinned: atBottom };
}

/* Node (tests) and the browser both, without a build step. */
if (typeof module !== "undefined" && module.exports) {
  module.exports = { shouldFollow, isAtBottom, BOTTOM_THRESHOLD_PX };
}
if (typeof window !== "undefined") {
  window.AriaScrollPolicy = { shouldFollow, isAtBottom, BOTTOM_THRESHOLD_PX };
}
