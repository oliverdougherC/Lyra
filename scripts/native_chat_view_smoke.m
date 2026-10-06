// Real AppKit/WebKit regression checks, in a private offscreen test process.
// Compile with ARC and AppKit, WebKit, and ApplicationServices frameworks.
#import <ApplicationServices/ApplicationServices.h>
#include "../src-tauri/src/native_chat.m"

void lyra_native_chat_scroll_changed(const char *hostId, bool atBottom, double ratio) {
    (void)hostId; (void)atBottom; (void)ratio;
}

static unsigned failures = 0;
static unsigned webviewWheelEvents = 0;
@interface SmokeWebView : WKWebView <WKNavigationDelegate>
@property (nonatomic) BOOL contentNavigationFinished;
@property (nonatomic, strong) NSError *contentLoadError;
@end
@implementation SmokeWebView
- (void)scrollWheel:(NSEvent *)event {
    webviewWheelEvents++;
    [super scrollWheel:event];
}
- (void)webView:(WKWebView *)webView didStartProvisionalNavigation:(WKNavigation *)navigation {
    (void)webView; (void)navigation;
    self.contentNavigationFinished = NO;
    self.contentLoadError = nil;
}
- (void)webView:(WKWebView *)webView didFinishNavigation:(WKNavigation *)navigation {
    (void)webView; (void)navigation;
    self.contentNavigationFinished = YES;
}
- (void)webView:(WKWebView *)webView didFailNavigation:(WKNavigation *)navigation
     withError:(NSError *)error {
    (void)webView; (void)navigation;
    self.contentLoadError = error;
}
- (void)webView:(WKWebView *)webView didFailProvisionalNavigation:(WKNavigation *)navigation
     withError:(NSError *)error {
    (void)webView; (void)navigation;
    self.contentLoadError = error;
}
- (void)webViewWebContentProcessDidTerminate:(WKWebView *)webView {
    (void)webView;
    self.contentLoadError = [NSError errorWithDomain:@"NativeChatSmoke" code:1
        userInfo:@{NSLocalizedDescriptionKey: @"WebKit content process terminated"}];
}
@end

// Reproduce the observed Tao/WKWebView geometry without making a titled window
// active: the WebView fills its parent but its DOM begins below a safe-area inset.
@interface SmokeCoordinateWebView : SmokeWebView
@property (nonatomic) CGFloat topSafeInset;
@end
@implementation SmokeCoordinateWebView
- (NSRect)safeAreaRect {
    NSRect safe = self.bounds;
    safe.size.height -= self.topSafeInset;
    if (self.isFlipped) safe.origin.y += self.topSafeInset;
    return safe;
}
@end

static void check(BOOL condition, NSString *message) {
    printf("%s %s\n", condition ? "PASS" : "FAIL", message.UTF8String);
    fflush(stdout);
    if (!condition) failures++;
}

static void pump(double seconds) {
    NSDate *end = [NSDate dateWithTimeIntervalSinceNow:seconds];
    while (end.timeIntervalSinceNow > 0)
        [[NSRunLoop mainRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.01]];
}

static BOOL waitForSyntheticContent(SmokeWebView *view) {
    // A cold WebKit process can take seconds to initialize on the macOS runner.
    // Wait for navigation, then validate the loaded DOM, under one shared bound.
    double started = NSProcessInfo.processInfo.systemUptime;
    double deadline = started + 15;
    while (!view.contentNavigationFinished && !view.contentLoadError &&
           NSProcessInfo.processInfo.systemUptime < deadline) pump(0.01);
    __block BOOL contentReady = NO;
    __block BOOL contentChecked = NO;
    __block NSError *scriptError = nil;
    if (view.contentNavigationFinished && !view.contentLoadError) {
        [view evaluateJavaScript:@"document.readyState === 'complete' && document.getElementById('native-chat-smoke-sentinel')?.textContent === 'Synthetic transcript only'"
               completionHandler:^(id value, NSError *error) {
            scriptError = error;
            contentReady = !error && [value isKindOfClass:NSNumber.class] && [value boolValue];
            contentChecked = YES;
        }];
        while (!contentChecked && !view.contentLoadError &&
               NSProcessInfo.processInfo.systemUptime < deadline) pump(0.01);
    }
    if (!contentReady || view.contentLoadError) {
        printf("DIAGNOSTIC content-load elapsed=%.2fs navigationFinished=%d loading=%d progress=%.2f sentinelChecked=%d sentinelReady=%d url=%s error=%s\n",
               NSProcessInfo.processInfo.systemUptime - started, view.contentNavigationFinished,
               view.loading, view.estimatedProgress, contentChecked, contentReady,
               (view.URL.absoluteString ?: @"(none)").UTF8String,
               (view.contentLoadError.localizedDescription ?: scriptError.localizedDescription ?: @"navigation/DOM deadline expired or sentinel did not match").UTF8String);
        fflush(stdout);
    }
    return contentReady && !view.contentLoadError;
}

static WKWebView *sectionOfClass(Class viewClass) {
    WKWebViewConfiguration *configuration = [WKWebViewConfiguration new];
    configuration.websiteDataStore = WKWebsiteDataStore.nonPersistentDataStore;
    SmokeWebView *view = [[viewClass alloc] initWithFrame:NSMakeRect(0, 0, 600, 500)
                                     configuration:configuration];
    // Match Wry's child view before production attachment reparents it.
    view.autoresizingMask = NSViewMinYMargin;
    view.navigationDelegate = view;
    [view loadHTMLString:@"<html><body style='margin:0;overflow:hidden'><div id='native-chat-smoke-sentinel' style='height:1800px;background:#eed'>Synthetic transcript only</div></body></html>"
                baseURL:nil];
    return view;
}

static WKWebView *section(void) { return sectionOfClass(SmokeWebView.class); }

@interface SmokeFlippedView : NSView
@end
@implementation SmokeFlippedView
- (BOOL)isFlipped { return YES; }
@end

static NSEvent *wheel(NSWindow *window, NSPoint point, int deltaY, int deltaX) {
    CGEventRef raw = CGEventCreateScrollWheelEvent(NULL, kCGScrollEventUnitPixel, 2,
                                                 deltaY, deltaX);
    NSPoint screen = [window convertPointToScreen:point];
    CGFloat screenHeight = NSScreen.screens.firstObject.frame.size.height;
    CGEventSetLocation(raw, CGPointMake(screen.x, screenHeight - screen.y));
    CGEventSetIntegerValueField(raw, kCGMouseEventWindowUnderMousePointer, window.windowNumber);
    CGEventSetIntegerValueField(raw, kCGMouseEventWindowUnderMousePointerThatCanHandleThisEvent,
                               window.windowNumber);
    NSEvent *event = [NSEvent eventWithCGEvent:raw];
    CFRelease(raw);
    return event;
}

static NSEvent *currentEvent(NSEvent *event) {
    // This is this test application's private queue, not CGEventPost or desktop
    // input. nextEventMatchingMask establishes NSApp.currentEvent exactly as the
    // native event loop does before window hit testing.
    [NSApp postEvent:event atStart:YES];
    NSEvent *dequeued = [NSApp nextEventMatchingMask:NSEventMaskFromType(event.type)
                                         untilDate:[NSDate dateWithTimeIntervalSinceNow:1]
                                            inMode:NSDefaultRunLoopMode dequeue:YES];
    check(dequeued.type == event.type && NSApp.currentEvent == dequeued,
          @"private AppKit queue establishes current event");
    return dequeued;
}

int main(void) {
    @autoreleasepool {
        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyProhibited];
        NSWindow *window = [[NSWindow alloc] initWithContentRect:NSMakeRect(-4000, -4000, 800, 700)
                                                      styleMask:NSWindowStyleMaskBorderless
                                                        backing:NSBackingStoreBuffered defer:NO];
        window.releasedWhenClosed = NO;
        [window orderBack:nil];
        SmokeCoordinateWebView *anchor = (SmokeCoordinateWebView *)sectionOfClass(SmokeCoordinateWebView.class);
        // Also cover a genuinely inset embedded WebView. The full-size WebView
        // with a title-bar safe area is tested independently below.
        anchor.frame = NSMakeRect(0, 0, 800, 668);
        anchor.autoresizingMask = NSViewNotSizable;
        [window.contentView addSubview:anchor];
        WKWebView *first = section();
        void *raw = lyra_native_chat_attach((__bridge void *)window, (__bridge void *)first,
                                           (__bridge void *)anchor,
                                           40, 100, 600, 500, "native-smoke-host");
        check(raw != NULL, @"production native transcript attaches");
        if (!raw) return 1;
        NSScrollView *scroll = (__bridge NSScrollView *)raw;
        check(fabs(NSMaxY(scroll.frame) - (668 - 100)) < 0.5,
              @"viewport attaches below the main WebView's 32-point top inset");
        check(scroll.clipsToBounds, @"outer scroll view clips transcript below the header");
        check(scroll.layer.masksToBounds && scroll.contentView.layer.masksToBounds,
              @"viewport backing layers contain hosted WebKit painting");
        check(scroll.documentView.clipsToBounds, @"document clips its section rendering");
        check(first.superview.clipsToBounds, @"first section clips its WebKit rendering");
        check(scroll.documentView.layer.masksToBounds && first.superview.layer.masksToBounds,
              @"document and section backing layers clip hosted content");
        lyra_native_chat_set_visible(raw, true);
        check(waitForSyntheticContent((SmokeWebView *)first),
              @"real nonpersistent WKWebView loads synthetic content");
        lyra_native_chat_set_section_height(raw, 0, 90);
        lyra_native_chat_set_frame(raw, 40, 100, 600, 300);
        check(fabs(NSMaxY(scroll.frame) - (668 - 100)) < 0.5,
              @"viewport resize keeps the main WebView's coordinate origin");
        check(fabs(scroll.documentView.frame.size.height - scroll.contentSize.height) < 1,
              @"short transcript does not acquire blank scrolling after height-only shrink");
        WKWebView *second = section();
        check(lyra_native_chat_add_section(raw, (__bridge void *)second), @"second section attaches");
        check(second.superview.clipsToBounds, @"additional sections clip their WebKit rendering");
        check(second.superview.layer.masksToBounds, @"additional section backing layer clips");
        lyra_native_chat_set_section_height(raw, 1, 100);
        check(fabs(scroll.documentView.frame.size.height - scroll.contentSize.height) < 1,
              @"short sections use measured heights rather than a viewport each");
        lyra_native_chat_remove_last_section(raw);
        lyra_native_chat_set_section_height(raw, 0, 1800);
        lyra_native_chat_set_scroll_ratio(raw, 0.5);
        pump(0.2);
        check(scroll.contentView.clipsToBounds, @"native clip view contains transcript painting");
        NSPoint local = NSMakePoint(NSMinX(scroll.contentView.bounds) + 120,
                                    NSMinY(scroll.contentView.bounds) + 100);
        NSPoint point = [scroll.contentView convertPoint:local toView:nil];
        NSPoint contentPoint = [window.contentView convertPoint:point fromView:nil];
        NSEvent *click = [NSEvent mouseEventWithType:NSEventTypeLeftMouseDown location:point
                                     modifierFlags:0 timestamp:NSProcessInfo.processInfo.systemUptime
                                       windowNumber:window.windowNumber context:nil eventNumber:1
                                         clickCount:1 pressure:1];
        click = currentEvent(click);
        NSView *hit = [window.contentView hitTest:contentPoint];
        printf("DIAGNOSTIC hit=%s outerOffset=%.1f windowPoint=(%.1f,%.1f)\n",
               NSStringFromClass(hit.class).UTF8String, scroll.contentView.bounds.origin.y,
               point.x, point.y);
        check(hit != nil && [hit isDescendantOf:first], @"click target preserves real WebKit interaction");
        CGFloat before = scroll.contentView.bounds.origin.y;
        NSEvent *horizontal = currentEvent(wheel(window, point, 0, -160));
        (void)horizontal;
        hit = [window.contentView hitTest:contentPoint];
        check(hit != nil && [hit isDescendantOf:first],
              @"horizontal wheel preserves WebKit code and table scrolling");
        NSEvent *event = wheel(window, point, -160, 0);
        event = currentEvent(event);
        hit = [window.contentView hitTest:contentPoint];
        // Offscreen CGEvents have no NSEvent.window; use AppKit's actual hit-test
        // result as the responder entry point. WebKit and its enclosing native
        // responder chain own the event, never a test-only scroll setter.
        [hit scrollWheel:event];
        pump(1.0);
        printf("DIAGNOSTIC wheelEvents=%u eventWindow=%ld expectedWindow=%ld eventPoint=(%.1f,%.1f)\n",
               webviewWheelEvents, (long)event.windowNumber, (long)window.windowNumber,
               event.locationInWindow.x, event.locationInWindow.y);
        check(hit != nil, @"wheel has a native responder target");
        check(fabs(scroll.contentView.bounds.origin.y - before) > 10,
              @"wheel over WebKit moves owning AppKit transcript");
        CGFloat controlBefore = scroll.contentView.bounds.origin.y;
        [scroll scrollWheel:event];
        pump(0.3);
        check(fabs(scroll.contentView.bounds.origin.y - controlBefore) > 10,
              @"control: identical wheel event can scroll AppKit directly");
        lyra_native_chat_set_scroll_ratio(raw, 0.5);
        // The child DOM captures these keys and sends a validated Rust action;
        // frontend event regressions cover that side. Here exercise the exact
        // native helper reached after validation, not WKWebView's old dead end.
        CGFloat keyBefore = scroll.contentView.bounds.origin.y;
        lyra_native_chat_scroll_key(raw, 0);
        check(scroll.contentView.bounds.origin.y < keyBefore - 10,
              @"validated Page Up action scrolls the native transcript");
        keyBefore = scroll.contentView.bounds.origin.y;
        lyra_native_chat_scroll_key(raw, 1);
        check(scroll.contentView.bounds.origin.y > keyBefore + 10,
              @"validated Page Down action scrolls the native transcript");
        lyra_native_chat_scroll_key(raw, 2);
        check(scroll.contentView.bounds.origin.y <= 1,
              @"validated Home action reaches transcript beginning");
        lyra_native_chat_scroll_key(raw, 3);
        check(fabs(scroll.contentView.bounds.origin.y - bottomOffset(scroll)) < 1,
              @"validated End action reaches transcript end");
        lyra_native_chat_set_visible(raw, false);
        lyra_native_chat_scroll_key(raw, 2);
        check(fabs(scroll.contentView.bounds.origin.y - bottomOffset(scroll)) < 1,
              @"hidden transcript ignores delayed keyboard actions");
        lyra_native_chat_set_visible(raw, true);
        lyra_native_chat_scroll_key(raw, 99);
        check(fabs(scroll.contentView.bounds.origin.y - bottomOffset(scroll)) < 1,
              @"unknown keyboard actions cannot move the transcript");
        anchor.pageZoom = 1.25;
        lyra_native_chat_set_frame(raw, 40, 100, 400, 250);
        check(fabs(scroll.frame.origin.x - 50) < 0.5 &&
              fabs(NSMaxY(scroll.frame) - (668 - 125)) < 0.5 &&
              fabs(scroll.frame.size.width - 500) < 0.5 &&
              fabs(scroll.frame.size.height - 312.5) < 0.5,
              @"zoomed DOM geometry scales within the main WebView coordinate origin");
        check(fabs(first.pageZoom - 1.25) < 0.001,
              @"transcript section shares the main WebView page zoom");
        lyra_native_chat_set_section_height(raw, 0, 1800);
        check(fabs(scroll.documentView.frame.size.height - 2250) < 0.5,
              @"measured CSS content height scales once at native page zoom");
        check(lyra_native_chat_add_section(raw, (__bridge void *)second),
              @"section can be added while main WebView is zoomed");
        lyra_native_chat_set_section_height(raw, 1, 64);
        check(fabs(second.pageZoom - 1.25) < 0.001 &&
              fabs(second.frame.size.height - 80) < 0.5 &&
              fabs(scroll.documentView.frame.size.height - 2330) < 0.5,
              @"new section inherits zoom without multiplying prior section heights again");
        lyra_native_chat_remove_last_section(raw);
        anchor.pageZoom = 1;
        lyra_native_chat_set_frame(raw, 40, 100, 600, 300);
        check(fabs(first.pageZoom - 1) < 0.001 &&
              fabs(scroll.documentView.frame.size.height - 1800) < 0.5,
              @"resetting page zoom restores measured CSS height exactly once");
        lyra_native_chat_detach(raw);

        anchor.frame = NSMakeRect(0, 0, 800, 700);
        anchor.topSafeInset = 32;
        check(anchor.isFlipped && fabs(anchor.safeAreaRect.origin.y - 32) < 0.5,
              @"full-size flipped WKWebView reproduces observed title-bar safe area");
        raw = lyra_native_chat_attach((__bridge void *)window, (__bridge void *)first,
                                      (__bridge void *)anchor,
                                      40, 100, 600, 300, "native-smoke-safe-area");
        scroll = (__bridge NSScrollView *)raw;
        check(fabs(NSMaxY(scroll.frame) - (700 - 32 - 100)) < 0.5,
              @"full-size WebView safe area offsets initial DOM viewport below title bar");
        lyra_native_chat_set_frame(raw, 40, 56, 600, 300);
        check(fabs(NSMaxY(scroll.frame) - (700 - 32 - 56)) < 0.5,
              @"DOM header height is added after the native safe-area inset");
        anchor.pageZoom = 1.25;
        lyra_native_chat_set_frame(raw, 40, 56, 400, 250);
        check(fabs(NSMaxY(scroll.frame) - (700 - 32 - 70)) < 0.5 &&
              fabs(scroll.frame.origin.x - 50) < 0.5 &&
              fabs(scroll.frame.size.height - 312.5) < 0.5,
              @"page zoom scales DOM bounds without scaling native safe-area inset");
        anchor.topSafeInset = 0;
        lyra_native_chat_set_frame(raw, 40, 56, 400, 250);
        check(fabs(NSMaxY(scroll.frame) - (700 - 70)) < 0.5,
              @"fullscreen zero safe area removes the title-bar offset at current zoom");
        anchor.topSafeInset = 32;
        anchor.pageZoom = 1;
        lyra_native_chat_set_frame(raw, 40, 56, 400, 250);
        check(fabs(NSMaxY(scroll.frame) - (700 - 32 - 56)) < 0.5,
              @"leaving fullscreen restores safe-area alignment without a stale offset");
        lyra_native_chat_detach(raw);

        window.contentView = [[SmokeFlippedView alloc] initWithFrame:NSMakeRect(0, 0, 800, 700)];
        anchor.pageZoom = 1;
        anchor.topSafeInset = 0;
        anchor.frame = NSMakeRect(0, 32, 800, 668);
        [window.contentView addSubview:anchor];
        raw = lyra_native_chat_attach((__bridge void *)window, (__bridge void *)first,
                                      (__bridge void *)anchor,
                                      40, 100, 600, 300, "native-smoke-flipped");
        scroll = (__bridge NSScrollView *)raw;
        check(fabs(scroll.frame.origin.y - 132) < 1,
              @"attachment respects inset anchor inside flipped parent");
        lyra_native_chat_set_frame(raw, 40, 120, 600, 250);
        check(fabs(scroll.frame.origin.y - 152) < 1,
              @"frame updates respect inset anchor inside flipped parent");
        lyra_native_chat_detach(raw);
        [first stopLoading];
        [second stopLoading];
        [anchor stopLoading];
        [window close];
        printf("RESULT failures=%u\n", failures);
        return failures ? 1 : 0;
    }
}
