import { Component, Suspense, createElement, lazy, useMemo, useState, type ComponentType, type ReactNode } from "react";

class PageErrorBoundary extends Component<{
  children: ReactNode;
  onRetry: () => void;
}, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (this.state.failed) {
      return <section className="deferred-page" role="alert">
        <h2>页面暂时无法打开</h2>
        <p>请检查连接后重试。如果服务刚刚更新，可以重新加载控制台。</p>
        <button onClick={this.props.onRetry}>重试页面</button>{" "}
        <button onClick={() => window.location.reload()}>重新加载控制台</button>
      </section>;
    }
    return this.props.children;
  }
}

/** A fresh lazy instance on retry avoids React caching a rejected loader forever.
 * Only the selected page mounts; the surrounding navigation remains available.
 */
export function deferredPage<P extends object>(load: () => Promise<{ default: ComponentType<P> }>) {
  return function DeferredPage(props: P) {
    const [attempt, setAttempt] = useState(0);
    const Page = useMemo(() => lazy(async () => {
      const { default: Loaded } = await load();
      return { default: ({ pageProps }: { pageProps: P }) => createElement(Loaded, pageProps) };
    }), [attempt]);
    return <PageErrorBoundary key={attempt} onRetry={() => setAttempt(value => value + 1)}>
      <Suspense fallback={<div className="deferred-page" role="status">正在加载页面…</div>}>
        <Page pageProps={props} />
      </Suspense>
    </PageErrorBoundary>;
  };
}
