// @vitest-environment jsdom
// issue #42：主动行为控制面板——全局字段/来源徽标、保存与恢复默认、渠道档位。
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import Initiative from "../Initiative";
import { setToken } from "../../api";
import {
  fetchCalls,
  installDomMocks,
  jsonResult,
  mockStudioFetch,
} from "../Studio/__tests__/helpers";

installDomMocks();

const INIT_VIEW = {
  global: {
    fields: {
      daily_limit: {
        value: 5,
        source: "console",
        note: "主动消息（提醒/主动搭话）每日上限；被动回复不受影响",
      },
      passive_daily_limit: {
        value: 200,
        source: "default",
        note: "被动回复每日保险丝（防跑飞）；0 需谨慎",
      },
      quiet_start: {
        value: "23:00",
        source: "console",
        note: "静默开始（本地时）；静默期内不主动打扰",
      },
      quiet_end: {
        value: "07:30",
        source: "default",
        note: "静默结束（本地时）",
      },
    },
    quiet_hours_rule:
      "静默时段只压制主动打扰；你主动发消息时鲸鱼照常回应",
    editable: ["daily_limit", "passive_daily_limit", "quiet_end", "quiet_start"],
    locked_by_deploy: ["enabled", "allowed_channels", "auto_send", "allow_calls"],
  },
  rhythm: {
    channels: {
      qq: { activity: "balanced", max_segments: 0, dnd: false },
    },
    conversations: [
      {
        conversation_key: "qq:group:g1",
        activity: "lively",
        max_segments: 3,
        dnd: true,
        version: 2,
      },
    ],
    activity_modes: ["quiet", "balanced", "lively"],
  },
  recent_decisions: [
    {
      trace_id: "tr-9",
      at: 1760000000,
      conversation_id: "group:g1",
      status: "silent",
      reason_code: "quiet_hours",
      reason_text: "静默时段，不打扰",
    },
  ],
};

function mockInitiative() {
  return mockStudioFetch({
    "/studio/initiative/global": (call) => {
      const body = (call.body ?? {}) as Record<string, unknown>;
      if (body.reset === true) return jsonResult({ global: INIT_VIEW.global });
      const fields: Record<string, { value: unknown; source: string; note: string }> =
        { ...INIT_VIEW.global.fields };
      for (const [k, v] of Object.entries(body)) {
        const old = fields[k];
        if (old) fields[k] = { ...old, value: v, source: "console" };
      }
      return jsonResult({ global: { ...INIT_VIEW.global, fields } });
    },
    "/studio/initiative/channel": (call) => {
      const body = (call.body ?? {}) as Record<string, unknown>;
      return jsonResult({
        rhythm: {
          ...INIT_VIEW.rhythm,
          channels: {
            qq: {
              activity: String(body.activity ?? ""),
              max_segments: Number(body.max_segments ?? 0),
              dnd: body.dnd === true,
            },
          },
        },
      });
    },
    "/studio/initiative": () => jsonResult(INIT_VIEW),
  });
}

beforeEach(() => {
  cleanup();
  setToken("test-token");
  vi.stubGlobal("confirm", vi.fn(() => true));
});

describe("Initiative 主动行为控制面板（issue #42）", () => {
  it("加载渲染：四字段当前值 + 来源徽标 + 静默规则 + 锁定字段 + 决策联动事件页", async () => {
    mockInitiative();
    const onNavigate = vi.fn();
    render(<Initiative onNavigate={onNavigate} />);

    // 四字段当前值
    await waitFor(() =>
      expect(
        (screen.getByLabelText("主动消息每日上限") as HTMLInputElement).value,
      ).toBe("5"),
    );
    expect(
      (screen.getByLabelText("被动回复每日保险丝") as HTMLInputElement).value,
    ).toBe("200");
    expect(
      (screen.getByLabelText("静默开始时间") as HTMLInputElement).value,
    ).toBe("23:00");
    expect(
      (screen.getByLabelText("静默结束时间") as HTMLInputElement).value,
    ).toBe("07:30");
    // 来源徽标：自定义×2（console）、默认×2（default）
    expect(screen.getAllByText("自定义")).toHaveLength(2);
    expect(screen.getAllByText("默认")).toHaveLength(2);
    // 影响说明 note
    expect(screen.getByText(/被动回复不受影响/)).toBeTruthy();
    // 静默规则固定文案
    expect(screen.getByText(/静默时段只压制主动打扰/)).toBeTruthy();
    // 部署侧锁定（只读说明）
    expect(screen.getByText(/部署侧锁定/)).toBeTruthy();
    expect(screen.getByText(/主动行为总开关（enabled）/)).toBeTruthy();
    // 会话级节奏只读
    expect(screen.getByText("qq:group:g1")).toBeTruthy();
    expect(screen.getAllByText("积极").length).toBeGreaterThanOrEqual(1);
    // 最近决策 + trace 跳事件页
    expect(screen.getByText("静默时段，不打扰")).toBeTruthy();
    fireEvent.click(screen.getByText("tr-9"));
    expect(onNavigate).toHaveBeenCalledWith("events");
  });

  it("保存：只提交改动字段的 PUT body；恢复默认走 reset", async () => {
    const mock = mockInitiative();
    render(<Initiative />);

    await waitFor(() =>
      expect(
        (screen.getByLabelText("主动消息每日上限") as HTMLInputElement).value,
      ).toBe("5"),
    );
    fireEvent.change(screen.getByLabelText("主动消息每日上限"), {
      target: { value: "8" },
    });
    fireEvent.click(screen.getByText("保存全局设置"));

    await waitFor(() => {
      const put = fetchCalls(mock).find(
        (c) =>
          c.method === "PUT" && c.url.includes("/studio/initiative/global"),
      );
      expect(put).toBeTruthy();
      expect(put!.body).toEqual({ daily_limit: 8 });
    });
    await screen.findByText("已保存全局主动行为设置。");

    fireEvent.click(screen.getByText("恢复默认"));
    await waitFor(() => {
      const puts = fetchCalls(mock).filter(
        (c) =>
          c.method === "PUT" && c.url.includes("/studio/initiative/global"),
      );
      expect(puts).toHaveLength(2);
      expect(puts[1].body).toEqual({ reset: true });
    });
    await screen.findByText("已恢复默认。");
  });

  it("渠道档位：设置 quiet 保存；改回「跟随内置默认」即清除覆盖", async () => {
    const mock = mockInitiative();
    render(<Initiative />);

    await waitFor(() =>
      expect(screen.getByLabelText("qq 档位")).toBeTruthy(),
    );
    // 设置 quiet
    fireEvent.change(screen.getByLabelText("qq 档位"), {
      target: { value: "quiet" },
    });
    fireEvent.click(screen.getByText("保存档位"));
    await waitFor(() => {
      const put = fetchCalls(mock).find(
        (c) =>
          c.method === "PUT" && c.url.includes("/studio/initiative/channel"),
      );
      expect(put).toBeTruthy();
      expect(put!.body).toEqual({
        channel: "qq",
        activity: "quiet",
        max_segments: 0,
        dnd: false,
      });
    });
    await screen.findByText(/已保存 QQ 渠道档位。/);

    // 清除覆盖：activity=""
    fireEvent.change(screen.getByLabelText("qq 档位"), {
      target: { value: "" },
    });
    fireEvent.click(screen.getByText("保存档位"));
    await waitFor(() => {
      const puts = fetchCalls(mock).filter(
        (c) =>
          c.method === "PUT" && c.url.includes("/studio/initiative/channel"),
      );
      expect(puts).toHaveLength(2);
      expect((puts[1].body as Record<string, unknown>).activity).toBe("");
    });
    await screen.findByText(/已清除 QQ 渠道覆盖/);
  });
});
