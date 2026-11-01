// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import MemoryView from "../Memory";
import { setToken } from "../../api";
import {
  fetchCalls,
  installDomMocks,
  jsonResult,
  mockStudioFetch,
} from "../Studio/__tests__/helpers";

installDomMocks();

const SUBJECTS = {
  owner: {
    spaces: [
      {
        space_id: "owner-main",
        space_type: "owner_main",
        name: "主工作库",
        paused: false,
        scope_note: "仅主人可见",
        record_count: 12,
      },
    ],
    vault_present: true,
  },
  members: [
    {
      principal_id: "qq:user:1001",
      space_id: "mem-1001",
      record_count: 5,
      opted_out: false,
      consent_active: true,
      paused: false,
      scope_note: "仅本人与主人可见",
    },
    {
      principal_id: "qq:user:1002",
      space_id: "mem-1002",
      record_count: 0,
      opted_out: true,
      consent_active: false,
      paused: true,
      scope_note: "仅本人与主人可见",
    },
  ],
  groups: [
    {
      space_id: "grp-1",
      space_type: "group_shared",
      name: "前端学习群",
      record_count: 30,
      paused: false,
      scope_note: "群内共享",
    },
  ],
  forks: [
    {
      space_id: "fork-1",
      space_type: "shareable_fork",
      name: "课程副本",
      record_count: 3,
      paused: false,
      scope_note: "可分享副本",
    },
  ],
  legacy_unscoped_records: 7,
};

const OWNER_BROWSE = {
  space_ids: ["owner-main"],
  metadata_only: false,
  total: 1,
  offset: 0,
  limit: 50,
  items: [
    {
      key: "mem:abc",
      kind: "episode",
      space_id: "owner-main",
      space_type: "owner_main",
      scope_note: "仅主人可见",
      created_at: "2026-10-02T10:00:00+00:00",
      superseded: false,
      confidence: 0.9,
      source: "chat",
      source_group: "",
      expires_at: null,
      last_confirmed: null,
      summary: "主人喜欢黑咖啡",
      tags: [],
    },
  ],
};

const MEMBER_BROWSE = {
  space_ids: ["mem-1001"],
  metadata_only: false,
  total: 1,
  offset: 0,
  limit: 50,
  items: [
    {
      key: "mem:k1",
      kind: "preference",
      space_id: "mem-1001",
      space_type: "member_private",
      scope_note: "仅本人与主人可见",
      created_at: "2026-10-01T08:00:00+00:00",
      superseded: false,
      confidence: 0,
      source: "chat",
      source_group: "",
      expires_at: null,
      last_confirmed: null,
      summary: "不吃辣",  // owner 决策：管理面全量可见
    },
  ],
};

beforeEach(() => {
  cleanup();
  setToken("test-token");
  vi.stubGlobal("confirm", vi.fn(() => true));
  vi.stubGlobal("prompt", vi.fn(() => ""));
});

describe("MemoryView 主体优先记忆管理", () => {
  it("加载主体总览：渲染主人/群友/群聊/其他空间四区及状态徽标", async () => {
    mockStudioFetch({
      "/studio/memory/subjects": () => jsonResult(SUBJECTS),
    });
    render(<MemoryView />);

    await screen.findByText("主工作库");
    expect(screen.getByText("主人")).toBeTruthy();
    expect(screen.getByText("群友")).toBeTruthy();
    expect(screen.getByText("群聊")).toBeTruthy();
    expect(screen.getByText("其他空间")).toBeTruthy();
    expect(screen.getByText("qq:user:1001")).toBeTruthy();
    expect(screen.getByText("前端学习群")).toBeTruthy();
    expect(screen.getByText("课程副本")).toBeTruthy();
    expect(screen.getByText(/原始库（受限）/)).toBeTruthy();
    expect(screen.getByText(/未分区遗留记录：\s*7/)).toBeTruthy();
    // 群友状态徽标：已退出 + 已暂停
    expect(screen.getByText("已退出")).toBeTruthy();
    expect(screen.getByText("已暂停")).toBeTruthy();
    // 未选中主体前不发起 browse
    expect(screen.getByText(/选中上方任一主体/)).toBeTruthy();
  });

  it("选中群友：按 principal_id 浏览，管理面全量可见且可治理（owner 决策 2026-11-01）", async () => {
    const mock = mockStudioFetch({
      "/studio/memory/subjects": () => jsonResult(SUBJECTS),
      "/studio/memory/browse": () => jsonResult(MEMBER_BROWSE),
    });
    render(<MemoryView />);

    fireEvent.click(await screen.findByText("qq:user:1001"));

    await waitFor(() => {
      const calls = fetchCalls(mock).filter((c) =>
        c.url.includes("/studio/memory/browse"),
      );
      expect(calls.length).toBeGreaterThan(0);
      expect(calls[0].url).toContain("principal_id=qq%3Auser%3A1001");
      expect(calls[0].url).not.toContain("space_id=");
    });
    // 管理面全量可见：摘要渲染 + 治理操作可用
    expect(await screen.findByText("不吃辣")).toBeTruthy();
    expect(screen.getByText("mem:k1")).toBeTruthy();
    expect(screen.getByText("纠错")).toBeTruthy();
    expect(screen.getByText("标记过期")).toBeTruthy();
  });

  it("选中主人空间：渲染记录摘要，纠错按钮提交正确 body 并提示新旧 key", async () => {
    const mock = mockStudioFetch({
      "/studio/memory/subjects": () => jsonResult(SUBJECTS),
      "/studio/memory/browse": () => jsonResult(OWNER_BROWSE),
      "/studio/memory/correct": () =>
        jsonResult({
          corrected: true,
          old_key: "mem:abc",
          new_key: "mem:corr-1",
        }),
    });
    vi.stubGlobal("prompt", vi.fn(() => "主人喜欢拿铁"));
    render(<MemoryView />);

    fireEvent.click(await screen.findByText("主工作库"));
    await screen.findByText("主人喜欢黑咖啡");
    // browse 用 space_id 选择器
    const browseCalls = fetchCalls(mock).filter((c) =>
      c.url.includes("/studio/memory/browse"),
    );
    expect(browseCalls[0].url).toContain("space_id=owner-main");

    fireEvent.click(screen.getByText("纠错"));

    await waitFor(() => {
      const post = fetchCalls(mock).find(
        (c) => c.method === "POST" && c.url.includes("/studio/memory/correct"),
      );
      expect(post).toBeTruthy();
      expect(post!.body).toEqual({
        key: "mem:abc",
        new_summary: "主人喜欢拿铁",
      });
    });
    await screen.findByText(/已纠正：mem:abc → mem:corr-1/);
  });

  it("暂停开关：对空间调用 POST /studio/memory/pause", async () => {
    const mock = mockStudioFetch({
      "/studio/memory/subjects": () => jsonResult(SUBJECTS),
      "/studio/memory/pause": () =>
        jsonResult({ space_id: "owner-main", paused: true }),
    });
    render(<MemoryView />);

    await screen.findByText("主工作库");
    // 第一个「暂停记录」按钮属于主人空间（owner 分组排在最前）
    fireEvent.click(screen.getAllByText("暂停记录")[0]);

    await waitFor(() => {
      const post = fetchCalls(mock).find(
        (c) => c.method === "POST" && c.url.includes("/studio/memory/pause"),
      );
      expect(post).toBeTruthy();
      expect(post!.body).toEqual({ space_id: "owner-main", paused: true });
    });
    await screen.findByText(/已暂停记录：主工作库/);
  });
});
