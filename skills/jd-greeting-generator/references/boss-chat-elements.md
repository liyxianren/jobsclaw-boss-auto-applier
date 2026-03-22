# BOSS Chat Elements

## Page Types

### 1. Job Detail Page
Expected URL shape:
- `/job_detail/<jobId>.html`

This page is where the sender looks for the first contact CTA.

### 2. Chat Page
Expected URL shape:
- `/web/geek/chat?...&jobId=<jobId>`

This page is where the sender validates target context, fills the message, and clicks send.

## CTA Button States on Job Detail

### `立即沟通` / `打招呼`
Meaning:
- first-contact action is available
- sender should click and then wait for chat navigation

### `继续沟通`
Meaning:
- there is already an existing conversation
- sender should treat the page as an existing chat path, not a fresh first-contact path

## Popup Layer
After clicking `立即沟通`, BOSS may show a popup with `继续沟通`.
The sender has a fast path that searches popup-like layers and clicks that CTA before falling back to slower recovery.

## Chat Context Elements
The sender validates that the current chat belongs to the target job.
It extracts:
- recruiter name from `.name-text`
- company from header spans or the active sidebar item
- title from `.position-list .position-name` or equivalent fallback selectors

This prevents sending a message into the wrong conversation.

## Message Input Selectors
Preferred input selectors:
- `#chat-input`
- `.chat-input[contenteditable="true"]`
- `.chat-input textarea`
- `.chat-input [contenteditable="true"]`
- fallback textareas / editable areas

`#chat-input` is the highest-priority selector on the newer BOSS chat page.

## Send Button Role
The sender first tries the exact BOSS send button path, then broader button selectors, then Enter fallback.
A send is only accepted after post-send verification.

## Post-send Verification
The sender verifies by combining multiple signals:
- message list count increased
- latest message prefix matches the outgoing text
- input box cleared or changed

If strict verification fails but soft verification passes, the send is still accepted and logged as a soft success.

## Verification / Risk Signals
These are treated as captcha or risk-control indicators:
- page title contains `安全验证`, `人机验证`, `验证码`, `Security Check`
- URL contains `security-check`, `captcha`, `verify`
- DOM contains `.geetest_slider`, `.nc_wrapper`, `.verify-wrap`, captcha/verify iframes

## Recovery Actions Bound to Elements
- URL mismatch after open: reopen target job URL
- wrong chat session: switch via sidebar conversation list
- missing chat page after first click: reload detail page and re-evaluate CTA state
- popup `继续沟通`: click popup first, then validate chat session
