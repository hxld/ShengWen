<script setup lang="ts">
import { ref, onMounted, onUnmounted } from "vue";
import { wb, messageOf } from "../utils/workbench";
const props = defineProps<{
  section: "connections" | "models" | "storage" | "templates";
}>();
const emit = defineEmits<{ changed: [] }>();
const busy = ref(false),
  message = ref(""),
  failed = ref(false);
const account = ref<any>({}),
  catalog = ref<any>({ models: [], downloads: [], runtime: {} });
const settings = ref<any>({
  template: "course",
  templates: {},
  obsidian_dir: "",
  glossary: "",
});
const secret = ref(""),
  browser = ref("edge"),
  profile = ref(""),
  modelId = ref("small");
const qr = ref<any>(null);
let timer: ReturnType<typeof setInterval> | undefined;
let disposed = false,
  refreshing = false;
const sourceLabels: Record<string, string> = {
  migrated: "从旧配置迁移",
  manual: "手动导入",
  saved: "本机存储",
  qr: "扫码",
  env: "环境变量",
};
const labels: Record<string, string> = {
  disconnected: "未连接",
  unverified: "已保存，尚未验证",
  connected: "登录有效",
  expired: "登录已失效",
  network_error: "验证暂时不可用",
};
const phases: Record<string, string> = {
  downloading: "下载中",
  verifying: "校验中",
  complete: "校验完成",
  paused: "已暂停",
  failed: "失败",
  idle: "尚未加载",
  ready: "已加载",
  transcribing: "转录中",
  loading: "加载中",
};
const size = (n: number) => `${(Number(n || 0) / 1024 / 1024).toFixed(0)} MB`;
async function refresh() {
  if (refreshing || disposed) return;
  refreshing = true;
  try {
    if (props.section === "connections")
      account.value = (await wb.get("/connection")).data;
    if (props.section === "models")
      catalog.value = (await wb.get("/models")).data;
  } catch (e) {
    if (!disposed) {
      message.value = messageOf(e);
      failed.value = true;
    }
  } finally {
    refreshing = false;
  }
}
async function act(fn: () => Promise<unknown>, success = "操作成功") {
  if (busy.value) return;
  busy.value = true;
  message.value = "";
  failed.value = false;
  try {
    await fn();
    message.value = success;
    await refresh();
    emit("changed");
  } catch (e) {
    message.value = messageOf(e);
    failed.value = true;
  } finally {
    busy.value = false;
  }
}
async function importSecret() {
  await act(async () => {
    await wb.post("/connection/import", { value: secret.value });
    secret.value = "";
  }, "凭据已接收，请检查下方存储状态并验证连接");
}
async function chooseCookie(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0];
  if (f) secret.value = await f.text();
}
async function startQr() {
  await act(async () => {
    qr.value = (await wb.post("/connection/qr")).data;
  }, "请用 B 站客户端扫码并确认");
}
async function tick() {
  await refresh();
  if (qr.value && !busy.value) {
    try {
      const state = (await wb.get(`/connection/qr/${qr.value.id}`)).data.state;
      if (state === "done") {
        qr.value = null;
        message.value = "扫码连接成功，请验证登录";
        await refresh();
      } else if (state === "expired" || state.includes("timeout")) {
        qr.value = null;
        message.value = "二维码已过期，请重新生成";
      }
    } catch {
      qr.value = null;
      message.value = "二维码检查失败，请重试或使用手动导入";
    }
  }
}
onMounted(async () => {
  await refresh();
  try {
    settings.value = (await wb.get("/settings")).data;
  } catch (e) {
    message.value = messageOf(e);
  }
  if (!disposed) timer = setInterval(tick, 4000);
});
onUnmounted(() => {
  disposed = true;
  if (timer) clearInterval(timer);
});
</script>
<template>
  <section class="space-y-4">
    <p
      v-if="message"
      role="status"
      :class="
        failed ? 'bg-red-50 text-red-700' : 'bg-emerald-50 text-emerald-800'
      "
      class="rounded-xl p-3 text-sm break-words"
    >
      {{ message }}
    </p>
    <template v-if="section === 'connections'">
      <div class="rounded-2xl border bg-slate-50 p-5 space-y-3">
        <div class="flex justify-between">
          <h3 class="font-semibold">B 站账号连接</h3>
          <span class="text-xs rounded-full px-3 py-1 bg-white border">{{
            labels[account.status] || "加载中"
          }}</span>
        </div>
        <p class="text-sm">
          {{ account.account?.name || "尚未验证账号" }}
          <span v-if="account.account?.uid" class="text-slate-400"
            >UID {{ account.account.uid }}</span
          >
        </p>
        <p class="text-xs text-slate-500">
          来源：{{ sourceLabels[account.source] || account.source || "无" }} ·
          凭据：{{ account.masked || "未设置" }}
        </p>
        <p class="text-xs text-slate-600">
          凭据存储：{{ account.storage_label || '正在读取' }}
          <span v-if="account.has_cookie"> · {{ account.storage_persistent ? '已持久保存' : account.source === 'env' ? '来自环境变量' : '仅本次运行有效' }}</span>
        </p>
        <p v-if="account.storage_warning" role="status" class="text-xs text-amber-700 bg-amber-50 p-3 rounded-lg">{{ account.storage_warning }}</p>
        <p v-if="account.checked_at" class="text-xs text-slate-500">
          上次验证：{{ new Date(account.checked_at).toLocaleString() }}
        </p>
        <p class="text-xs text-slate-600">
          {{
            account.message ||
            "连接后，字幕与音视频下载共用此账号。登录有效不代表每个视频都有字幕。"
          }}
        </p>
        <div class="flex flex-wrap gap-2">
          <button :disabled="busy" class="btn" @click="startQr">扫码连接</button
          ><button
            :disabled="busy || !account.has_cookie"
            class="btn"
            @click="act(() => wb.post('/connection/verify'), '验证完成')"
          >
            验证连接</button
          ><button
            :disabled="busy || !account.has_cookie"
            class="btn text-red-600"
            @click="
              act(
                () => wb.delete('/connection'),
                '已移除连接并停用环境变量回退',
              )
            "
          >
            移除此连接
          </button>
        </div>
        <div v-if="qr" class="bg-white rounded-xl p-3 text-center">
          <img :src="qr.image" alt="B站登录二维码" class="w-48 h-48 mx-auto" />
          <p class="text-sm mt-2">
            用 B 站客户端扫码确认 · 二维码约 3 分钟有效
          </p>
        </div>
      </div>
      <details class="rounded-xl border p-4" open>
        <summary class="font-medium cursor-pointer">从浏览器或文件导入</summary>
        <div class="space-y-3 mt-3">
          <div class="flex gap-2">
            <select v-model="browser" class="input">
              <option value="edge">Edge</option>
              <option value="chrome">Chrome</option>
              <option value="firefox">Firefox</option>
              <option value="brave">Brave</option></select
            ><button
              :disabled="busy"
              class="btn shrink-0"
              @click="
                act(
                  () =>
                    wb.post('/connection/browser', {
                      browser,
                      profile: profile || null,
                    }),
                  '浏览器凭据已读取，请检查存储状态并验证连接',
                )
              "
            >
              读取浏览器
            </button>
          </div>
          <input
            v-model="profile"
            class="input"
            placeholder="可选：指定配置文件中的 Cookie 数据库绝对路径"
            aria-label="浏览器 Cookie 文件路径"
          />
          <label class="text-xs text-slate-600 block"
            >高级导入：SESSDATA 或完整 Cookie</label
          ><input
            v-model="secret"
            type="password"
            autocomplete="off"
            class="input"
            placeholder="通过当前系统安全存储保存；不可用时仅本次运行有效"
          />
          <div class="flex gap-3 items-center">
            <button
              :disabled="busy || !secret.trim()"
              class="btn"
              @click="importSecret"
            >
              保存凭据</button
            ><label class="text-sm cursor-pointer text-blue-600"
              >选择 Netscape 文件<input
                type="file"
                accept=".txt"
                class="hidden"
                @change="chooseCookie"
            /></label>
          </div>
          <p class="text-xs text-slate-500">
            保存失败时保留输入。网络错误不会清空凭据。导入会替换当前连接，保存后可单独验证。
          </p>
        </div>
      </details>
    </template>
    <template v-if="section === 'models'">
      <div class="flex justify-between items-center">
        <h3 class="font-semibold">本地模型库</h3>
        <span class="text-xs text-slate-500">{{
          phases[catalog.runtime?.phase] || catalog.runtime?.phase
        }}</span>
      </div>
      <p class="text-xs text-slate-500">
        测试完成后释放测试模型；开始转录时再按需加载。
      </p>
      <p v-if="catalog.runtime?.model" class="text-xs break-all">
        运行模型：{{ catalog.runtime.model }}
      </p>
      <div
        v-for="model in catalog.models"
        :key="model.path"
        class="rounded-xl border p-4 space-y-2"
        :class="model.active ? 'border-blue-300 bg-blue-50/50' : 'bg-white'"
      >
        <div class="flex justify-between">
          <b class="text-sm">{{ model.id }}</b
          ><span class="text-xs">{{
            model.active
              ? "当前选择"
              : model.complete
                ? "文件齐全"
                : "文件不完整"
          }}</span>
        </div>
        <p class="text-xs text-slate-500 break-all">{{ model.path }}</p>
        <p class="text-xs">
          {{ size(model.size) }} ·
          {{ model.verified ? "下载哈希校验通过" : "可执行本地加载测试" }}
        </p>
        <div class="flex gap-2">
          <button
            class="btn"
            :disabled="busy || !model.complete || model.active"
            @click="
              act(
                () => wb.post('/models/select', { id: model.id }),
                '模型已选择，在下一次转录时加载',
              )
            "
          >
            使用此模型</button
          ><button
            class="btn"
            :disabled="busy || !model.complete"
            @click="
              act(
                () =>
                  wb.post(
                    '/models/test',
                    { id: model.id },
                    { timeout: 120000 },
                  ),
                '离线加载测试通过',
              )
            "
          >
            测试加载
          </button>
        </div>
      </div>
      <div class="flex gap-2">
        <select v-model="modelId" class="input">
          <option
            v-for="m in ['tiny', 'base', 'small', 'medium', 'large-v3']"
            :key="m"
          >
            {{ m }}
          </option></select
        ><button
          class="btn shrink-0"
          :disabled="busy"
          @click="
            act(
              () => wb.post('/models/download', { id: modelId }),
              '已开始下载，可在下方查看进度',
            )
          "
        >
          下载 / 继续</button
        ><button
          class="btn shrink-0"
          :disabled="busy"
          @click="act(() => wb.post('/models/release'), '已释放模型显存')"
        >
          释放模型
        </button>
      </div>
      <div
        v-for="d in catalog.downloads"
        :key="d.id"
        class="text-sm border rounded-xl p-3"
      >
        <div class="flex justify-between">
          <b>{{ d.id }} · {{ phases[d.phase] || d.phase }}</b
          ><button
            v-if="d.phase === 'downloading'"
            class="text-blue-600"
            @click="
              act(
                () => wb.post('/models/pause', { id: d.id }),
                '正在停止下载，已完成分段会保留',
              )
            "
          >
            暂停
          </button>
        </div>
        <progress
          :value="d.downloaded"
          :max="d.total || 1"
          class="w-full mt-2"
        ></progress>
        <p>
          {{ size(d.downloaded) }} / {{ size(d.total) }} · {{ size(d.speed) }}/s
        </p>
        <p v-if="d.error" class="text-red-600">{{ d.error }}</p>
      </div>
    </template>
    <template v-if="section === 'templates' || section === 'storage'">
      <div class="rounded-xl border p-4 space-y-4">
        <template v-if="section === 'templates'"
          ><h3 class="font-semibold">输出目标</h3>
          <select v-model="settings.template" class="input">
            <option v-for="(v, k) in settings.templates" :value="k" :key="k">
              {{ v.label }}
            </option>
          </select>
          <p class="text-xs text-slate-500">
            模板决定笔记结构；自动 / 快速 /
            深度决定处理方式。新任务使用此默认模板，已有任务可单独重新生成。
          </p>
          <label class="block text-sm"
            >术语提示<textarea
              v-model="settings.glossary"
              rows="3"
              class="input mt-2"
              placeholder="人名、专有名词等，作为后续转录的提示"
            ></textarea></label
        ></template>
        <template v-else
          ><h3 class="font-semibold">Obsidian 导出目录</h3>
          <input
            v-model="settings.obsidian_dir"
            class="input"
            placeholder="绝对目录路径"
          />
          <p class="text-xs text-slate-500">
            文件名包含任务 ID；同名时另存新版本，保护人工修改。
          </p>
          <p class="text-sm">访问方式：本机优先</p>
          <p class="text-xs text-slate-500">
            监听地址：{{ settings.host }}。局域网 API 访问需要服务端环境变量
            SHENGWEN_ACCESS_TOKEN 和 Bearer 认证。
          </p></template
        >
        <button
          class="btn"
          :disabled="busy"
          @click="
            act(
              () =>
                wb.put('/settings', {
                  template: settings.template,
                  obsidian_dir: settings.obsidian_dir,
                  glossary: settings.glossary,
                }),
              '设置已保存',
            )
          "
        >
          保存本页
        </button>
      </div>
    </template>
  </section>
</template>
<style scoped>
.btn {
  @apply px-3 py-2 text-xs font-medium border border-slate-200 rounded-lg bg-white hover:bg-slate-100 disabled:opacity-40 disabled:cursor-not-allowed;
}
.input {
  @apply w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm;
}
</style>
