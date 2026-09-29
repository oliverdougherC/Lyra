#import <AppKit/AppKit.h>
#import <WebKit/WebKit.h>
#import <objc/runtime.h>
#include <stdbool.h>

extern void lyra_native_chat_scroll_changed(bool atBottom);

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
@property (nonatomic) BOOL lastBottom;
- (void)boundsChanged:(NSNotification *)notification;
- (void)notifyIfChanged;
@end

static LyraChatScrollObserver *observerFor(NSScrollView *scroll) {
    return objc_getAssociatedObject(scroll, @selector(boundsChanged:));
}

static NSScrollView *chatScroll(void *rawScroll) {
    return (__bridge NSScrollView *)rawScroll;
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
    if (atBottom == self.lastBottom) return;
    self.lastBottom = atBottom;
    lyra_native_chat_scroll_changed(atBottom);
}
@end

static void layoutSections(NSScrollView *scroll, bool follow) {
    LyraChatDocument *document = (LyraChatDocument *)scroll.documentView;
    if (!document) return;
    double width = scroll.contentSize.width;
    double y = 0;
    for (NSUInteger i = 0; i < document.sections.count; i++) {
        double height = document.heights[i].doubleValue;
        NSView *section = document.sections[i];
        section.frame = NSMakeRect(0, y, width, height);
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
void *lyra_native_chat_attach(void *rawWindow, void *rawWebview,
                              double x, double top, double width, double height) {
    NSWindow *window = (__bridge NSWindow *)rawWindow;
    WKWebView *webview = (__bridge WKWebView *)rawWebview;
    NSView *content = window.contentView;
    if (!window || !webview || !content || width <= 0 || height <= 0) return NULL;

    NSScrollView *scroll = [[NSScrollView alloc] initWithFrame:NSZeroRect];
    scroll.hasVerticalScroller = YES;
    scroll.hasHorizontalScroller = NO;
    scroll.scrollsDynamically = YES;
    scroll.drawsBackground = NO;
    scroll.hidden = YES;
    scroll.contentView.postsBoundsChangedNotifications = YES;
    LyraChatScrollObserver *observer = [LyraChatScrollObserver new];
    observer.scroll = scroll;
    observer.lastBottom = YES;
    objc_setAssociatedObject(scroll, @selector(boundsChanged:), observer, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
    [[NSNotificationCenter defaultCenter] addObserver:observer selector:@selector(boundsChanged:)
                                         name:NSViewBoundsDidChangeNotification object:scroll.contentView];
    LyraChatDocument *document = [[LyraChatDocument alloc] initWithFrame:NSMakeRect(0, 0, width, height)];
    LyraChatSection *firstSection = [[LyraChatSection alloc] initWithFrame:NSMakeRect(0, 0, width, height)];
    document.sections = [NSMutableArray arrayWithObject:firstSection];
    document.heights = [NSMutableArray arrayWithObject:@(height)];
    [webview removeFromSuperview];
    webview.frame = NSMakeRect(0, 0, width, height);
    [firstSection addSubview:webview];
    [document addSubview:firstSection];
    scroll.documentView = document;
    [content addSubview:scroll];
    scroll.frame = NSMakeRect(x, content.bounds.size.height - top - height, width, height);
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
    LyraChatSection *section = [[LyraChatSection alloc] initWithFrame:NSZeroRect];
    [document.sections addObject:section];
    [document.heights addObject:@(scroll.contentSize.height)];
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
    scroll.frame = NSMakeRect(x, parent.bounds.size.height - top - height, width, height);
    layoutSections(scroll, follow);
}

void lyra_native_chat_set_section_height(void *rawScroll, unsigned long index, double height) {
    NSScrollView *scroll = chatScroll(rawScroll);
    LyraChatDocument *document = (LyraChatDocument *)scroll.documentView;
    if (!document || index >= document.heights.count || height <= 0) return;
    bool follow = lyra_native_chat_near_bottom(rawScroll);
    document.heights[index] = @(MAX(height, scroll.contentSize.height));
    layoutSections(scroll, follow);
}

void lyra_native_chat_scroll_to_bottom(void *rawScroll) {
    NSScrollView *scroll = chatScroll(rawScroll);
    if (!scroll) return;
    [scroll.contentView scrollToPoint:NSMakePoint(0, bottomOffset(scroll))];
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
