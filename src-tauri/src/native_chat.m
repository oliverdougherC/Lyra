#import <AppKit/AppKit.h>
#import <WebKit/WebKit.h>
#import <QuartzCore/QuartzCore.h>
#import <objc/runtime.h>
#include <stdbool.h>
#include <math.h>

extern void lyra_native_chat_scroll_changed(const char *hostId, bool atBottom, double ratio);

@interface LyraChatScrollView : NSScrollView
@property (nonatomic, weak) NSView *coordinateView;
@end
@implementation LyraChatScrollView
- (NSView *)hitTest:(NSPoint)point {
    NSView *hit = [super hitTest:point];
    NSEvent *event = NSApp.currentEvent;
    // The section's non-scrolling WKWebView otherwise consumes vertical wheel
    // events. Keep AppKit in charge of gestures and momentum; ordinary clicks,
    // selection and horizontal scrolling still reach the rich content.
    if (hit && [hit isDescendantOf:self.contentView] && event.type == NSEventTypeScrollWheel &&
        !(event.modifierFlags & NSEventModifierFlagShift) &&
        fabs(event.scrollingDeltaY) >= fabs(event.scrollingDeltaX)) return self;
    return hit;
}
@end

@interface LyraChatDocument : NSView
@property (nonatomic, strong) NSMutableArray<NSView *> *sections;
@property (nonatomic, strong) NSMutableArray<NSNumber *> *heights;
@end

@interface LyraChatSection : NSView
@end
@implementation LyraChatSection
- (BOOL)isFlipped { return YES; }
@end
@implementation LyraChatDocument
- (BOOL)isFlipped { return YES; }
@end

@interface LyraChatScrollObserver : NSObject
@property (nonatomic, weak) NSScrollView *scroll;
@property (nonatomic, copy) NSString *hostId;
- (void)boundsChanged:(NSNotification *)notification;
- (void)notifyIfChanged;
@end

static LyraChatScrollObserver *observerFor(NSScrollView *scroll) {
    return objc_getAssociatedObject(scroll, @selector(boundsChanged:));
}

static NSScrollView *chatScroll(void *rawScroll) {
    return (__bridge NSScrollView *)rawScroll;
}

static void clipContent(NSView *view) {
    // WebKit paints through hosted layers. Clip the backing layer as well as
    // AppKit drawing so a scrolled section cannot cover the surrounding shell.
    view.wantsLayer = YES;
    view.clipsToBounds = YES;
    view.layer.masksToBounds = YES;
}

// A mount obtains this lease on the main thread before dispatching its child
// attachment, so closing the window cannot invalidate a borrowed WebKit pointer.
void *lyra_native_chat_retain_view(void *rawView) {
    return (__bridge_retained void *)(__bridge NSView *)rawView;
}
void lyra_native_chat_release_view(void *rawView) {
    NSView *view = (__bridge_transfer NSView *)rawView;
    (void)view;
}

static double contentZoom(NSScrollView *scroll) {
    NSView *anchor = ((LyraChatScrollView *)scroll).coordinateView;
    return [anchor isKindOfClass:WKWebView.class] ? ((WKWebView *)anchor).pageZoom : 1;
}

static NSRect viewportFrame(NSView *anchor, NSView *parent, double x, double top,
                             double width, double height, double zoom) {
    // DOM bounds originate in the main WebView, not the full-size Tao content
    // view (which also includes the title bar on macOS).
    // WKWebView's DOM viewport excludes the title-bar safe area even when its
    // native frame fills the window. This inset also changes in full screen.
    NSRect bounds = anchor.safeAreaRect;
    NSRect local = NSMakeRect(bounds.origin.x + x * zoom,
        anchor.isFlipped ? bounds.origin.y + top * zoom : NSMaxY(bounds) - (top + height) * zoom,
        width * zoom, height * zoom);
    return [anchor convertRect:local toView:parent];
}

static double bottomOffset(NSScrollView *scroll) {
    return MAX(0, scroll.documentView.frame.size.height - scroll.contentView.bounds.size.height);
}

bool lyra_native_chat_near_bottom(void *rawScroll) {
    NSScrollView *scroll = chatScroll(rawScroll);
    return scroll && bottomOffset(scroll) - scroll.contentView.bounds.origin.y <= 64;
}

@implementation LyraChatScrollObserver
- (void)boundsChanged:(NSNotification *)notification { [self notifyIfChanged]; }
- (void)notifyIfChanged {
    if (!self.scroll) return;
    BOOL atBottom = lyra_native_chat_near_bottom((__bridge void *)self.scroll);
    double bottom = bottomOffset(self.scroll);
    double ratio = bottom > 0 ? self.scroll.contentView.bounds.origin.y / bottom : 1;
    lyra_native_chat_scroll_changed(self.hostId.UTF8String, atBottom, MIN(1, MAX(0, ratio)));
}
@end

static void layoutSections(NSScrollView *scroll, bool follow) {
    LyraChatDocument *document = (LyraChatDocument *)scroll.documentView;
    if (!document) return;
    double width = scroll.contentSize.width;
    double zoom = contentZoom(scroll);
    double y = 0;
    for (NSUInteger i = 0; i < document.sections.count; i++) {
        double height = document.heights[i].doubleValue * zoom;
        NSView *section = document.sections[i];
        section.frame = NSMakeRect(0, y, width, height);
        ((WKWebView *)section.subviews.firstObject).pageZoom = zoom;
        section.subviews.firstObject.frame = NSMakeRect(0, 0, width, height);
        y += height;
    }
    document.frame = NSMakeRect(0, 0, width, MAX(y, scroll.contentSize.height));
    if (follow) {
        [scroll.contentView scrollToPoint:NSMakePoint(0, bottomOffset(scroll))];
        [scroll reflectScrolledClipView:scroll.contentView];
    }
    [observerFor(scroll) notifyIfChanged];
}

// AppKit owns the single scroll offset; short WKWebViews paint each rich section.
void *lyra_native_chat_attach(void *rawWindow, void *rawWebview, void *rawAnchor,
                              double x, double top, double width, double height, const char *hostId) {
    NSWindow *window = (__bridge NSWindow *)rawWindow;
    WKWebView *webview = (__bridge WKWebView *)rawWebview;
    NSView *anchor = (__bridge NSView *)rawAnchor;
    NSView *content = window.contentView;
    if (!window || !webview || !content || !anchor || anchor.window != window ||
        !hostId || width <= 0 || height <= 0) return NULL;

    NSScrollView *scroll = [[LyraChatScrollView alloc] initWithFrame:NSZeroRect];
    ((LyraChatScrollView *)scroll).coordinateView = anchor;
    clipContent(scroll);
    clipContent(scroll.contentView);
    scroll.hasVerticalScroller = YES;
    scroll.hasHorizontalScroller = NO;
    scroll.scrollsDynamically = YES;
    scroll.drawsBackground = NO;
    scroll.hidden = YES;
    scroll.contentView.postsBoundsChangedNotifications = YES;
    LyraChatScrollObserver *observer = [LyraChatScrollObserver new];
    observer.scroll = scroll;
    observer.hostId = [NSString stringWithUTF8String:hostId];
    objc_setAssociatedObject(scroll, @selector(boundsChanged:), observer, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
    [[NSNotificationCenter defaultCenter] addObserver:observer selector:@selector(boundsChanged:)
                                         name:NSViewBoundsDidChangeNotification object:scroll.contentView];
    LyraChatDocument *document = [[LyraChatDocument alloc] initWithFrame:NSMakeRect(0, 0, width, height)];
    LyraChatSection *firstSection = [[LyraChatSection alloc] initWithFrame:NSMakeRect(0, 0, width, height)];
    clipContent(document);
    clipContent(firstSection);
    document.sections = [NSMutableArray arrayWithObject:firstSection];
    document.heights = [NSMutableArray arrayWithObject:@(height)];
    [webview removeFromSuperview];
    webview.autoresizingMask = NSViewNotSizable;
    webview.frame = NSMakeRect(0, 0, width, height);
    [firstSection addSubview:webview];
    [document addSubview:firstSection];
    scroll.documentView = document;
    [content addSubview:scroll];
    scroll.frame = viewportFrame(anchor, content, x, top, width, height, contentZoom(scroll));
    layoutSections(scroll, true);
    return (__bridge_retained void *)scroll;
}

bool lyra_native_chat_add_section(void *rawScroll, void *rawWebview) {
    NSScrollView *scroll = chatScroll(rawScroll);
    WKWebView *webview = (__bridge WKWebView *)rawWebview;
    if (!scroll || !webview) return false;
    LyraChatDocument *document = (LyraChatDocument *)scroll.documentView;
    if (!document) return false;
    bool follow = lyra_native_chat_near_bottom(rawScroll);
    [webview removeFromSuperview];
    webview.autoresizingMask = NSViewNotSizable;
    LyraChatSection *section = [[LyraChatSection alloc] initWithFrame:NSZeroRect];
    clipContent(section);
    [document.sections addObject:section];
    [document.heights addObject:@(scroll.contentSize.height / contentZoom(scroll))];
    [section addSubview:webview];
    [document addSubview:section];
    layoutSections(scroll, follow);
    return true;
}

void lyra_native_chat_remove_last_section(void *rawScroll) {
    NSScrollView *scroll = chatScroll(rawScroll);
    LyraChatDocument *document = (LyraChatDocument *)scroll.documentView;
    if (document.sections.count <= 1) return;
    bool follow = lyra_native_chat_near_bottom(rawScroll);
    [document.sections.lastObject removeFromSuperview];
    [document.sections removeLastObject];
    [document.heights removeLastObject];
    layoutSections(scroll, follow);
}

void lyra_native_chat_set_frame(void *rawScroll, double x, double top,
                                double width, double height) {
    NSScrollView *scroll = chatScroll(rawScroll);
    if (!scroll || !scroll.superview || width <= 0 || height <= 0) return;
    bool follow = lyra_native_chat_near_bottom(rawScroll);
    NSView *parent = scroll.superview;
    NSView *anchor = ((LyraChatScrollView *)scroll).coordinateView;
    if (!anchor || anchor.window != scroll.window) return;
    scroll.frame = viewportFrame(anchor, parent, x, top, width, height, contentZoom(scroll));
    layoutSections(scroll, follow);
}

void lyra_native_chat_set_section_height(void *rawScroll, unsigned long index, double height) {
    NSScrollView *scroll = chatScroll(rawScroll);
    LyraChatDocument *document = (LyraChatDocument *)scroll.documentView;
    if (!document || index >= document.heights.count || height <= 0) return;
    bool follow = lyra_native_chat_near_bottom(rawScroll);
    document.heights[index] = @(height);
    layoutSections(scroll, follow);
}

void lyra_native_chat_scroll_to_bottom(void *rawScroll) {
    NSScrollView *scroll = chatScroll(rawScroll);
    if (!scroll) return;
    [scroll.contentView scrollToPoint:NSMakePoint(0, bottomOffset(scroll))];
    [scroll reflectScrolledClipView:scroll.contentView];
    [observerFor(scroll) notifyIfChanged];
}

void lyra_native_chat_set_scroll_ratio(void *rawScroll, double ratio) {
    NSScrollView *scroll = chatScroll(rawScroll);
    if (!scroll || !isfinite(ratio)) return;
    [scroll.contentView scrollToPoint:NSMakePoint(0, bottomOffset(scroll) * MIN(1, MAX(0, ratio)))];
    [scroll reflectScrolledClipView:scroll.contentView];
    [observerFor(scroll) notifyIfChanged];
}

void lyra_native_chat_set_visible(void *rawScroll, bool visible) {
    NSScrollView *scroll = chatScroll(rawScroll);
    if (scroll) scroll.hidden = !visible;
}

void lyra_native_chat_detach(void *rawScroll) {
    if (!rawScroll) return;
    NSScrollView *scroll = (__bridge_transfer NSScrollView *)rawScroll;
    LyraChatScrollObserver *observer = observerFor(scroll);
    [[NSNotificationCenter defaultCenter] removeObserver:observer];
    objc_setAssociatedObject(scroll, @selector(boundsChanged:), nil, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
    scroll.documentView = nil;
    [scroll removeFromSuperview];
}

// Child WebKit key events arrive through the authenticated snapshot action path.
// AppKit remains the only owner of the transcript's vertical position.
void lyra_native_chat_scroll_key(void *rawScroll, unsigned int key) {
    NSScrollView *scroll = chatScroll(rawScroll);
    if (!scroll || scroll.hidden) return;
    double bottom = bottomOffset(scroll);
    double position = scroll.contentView.bounds.origin.y;
    double page = MAX(1, scroll.contentView.bounds.size.height * 0.9);
    switch (key) {
        case 0: position -= page; break;
        case 1: position += page; break;
        case 2: position = 0; break;
        case 3: position = bottom; break;
        default: return;
    }
    [scroll.contentView scrollToPoint:NSMakePoint(0, MIN(bottom, MAX(0, position)))];
    [scroll reflectScrolledClipView:scroll.contentView];
    [observerFor(scroll) notifyIfChanged];
}
