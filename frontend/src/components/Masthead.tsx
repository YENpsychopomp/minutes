import type { ServiceConfig } from "../types";

export function Masthead({ config }: { config?: ServiceConfig }) {
  const asr = config?.asr;
  const llm = config?.llm;
  const offline = !config;
  const notConfigured = config && (!asr?.key_configured || !llm?.key_configured);
  return (
    <header className="masthead">
      <div className="masthead-inner">
        <div className="wordmark">
          summery<em>.</em>
        </div>
        <div className="tagline">影音轉逐字稿，再整理成一份讀得完整的摘要</div>
        <div className="masthead-right">
          {offline ? (
            <span className="chip chip-rust">
              <span className="dot" />
              連不到服務
            </span>
          ) : notConfigured ? (
            <span className="chip chip-amber">
              <span className="dot" />
              金鑰未設定
            </span>
          ) : (
            <span className="chip chip-sage">
              <span className="dot" />
              服務就緒
            </span>
          )}
          {asr ? <span className="num">{asr.model}</span> : null}
          {llm ? <span aria-hidden="true">·</span> : null}
          {llm ? <span className="num">{llm.model}</span> : null}
        </div>
      </div>
    </header>
  );
}
