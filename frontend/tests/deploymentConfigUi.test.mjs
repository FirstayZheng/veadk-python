import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const projectPreviewSource = readFileSync(
  new URL("../src/ui/ProjectPreview.tsx", import.meta.url),
  "utf8",
);
const projectPreviewStyles = readFileSync(
  new URL("../src/ui/ProjectPreview.css", import.meta.url),
  "utf8",
);
const customCreateSource = readFileSync(
  new URL("../src/create/CustomCreate.tsx", import.meta.url),
  "utf8",
);
const appSource = readFileSync(
  new URL("../src/App.tsx", import.meta.url),
  "utf8",
);
const appStyles = readFileSync(
  new URL("../src/styles.css", import.meta.url),
  "utf8",
);
const deploymentTaskIconsSource = readFileSync(
  new URL("../src/ui/icons/DeploymentTaskIcons.tsx", import.meta.url),
  "utf8",
);
const agentTypeMetaSource = readFileSync(
  new URL("../src/create/agentTypeMeta.tsx", import.meta.url),
  "utf8",
);

test("shares the create-page agent type icons with the deployment topology", () => {
  assert.match(customCreateSource, /from "\.\/agentTypeMeta"/);
  assert.match(projectPreviewSource, /from "\.\.\/create\/agentTypeMeta"/);
  assert.match(
    projectPreviewSource,
    /const meta = agentTypeMeta\(agent\.type\)/,
  );
  assert.doesNotMatch(projectPreviewSource, /function topologyIcon/);

  for (const icon of ["LlmIcon", "GitBranch", "Split", "Repeat", "Globe"]) {
    assert.match(agentTypeMetaSource, new RegExp(`icon: ${icon}`));
  }
});

test("offers the AgentKit-backed remote Agent type", () => {
  assert.match(agentTypeMetaSource, /label: "远程智能体"/);
  assert.match(
    agentTypeMetaSource,
    /export const AGENT_TYPES:[\s\S]*?AGENT_TYPE_META\.a2a/,
  );
  assert.match(customCreateSource, /AgentKit 智能体中心/);
  assert.match(
    customCreateSource,
    /remoteTypeDisabled = isRootAgent && t\.id === "a2a"/,
  );
});

test("places the add-variable row before any environment variable rows", () => {
  const addRowIndex = projectPreviewSource.indexOf('className="pp-env-add"');
  const tableIndex = projectPreviewSource.indexOf('className="pp-env-table"');

  assert.notEqual(addRowIndex, -1);
  assert.notEqual(tableIndex, -1);
  assert.ok(addRowIndex < tableIndex);
  assert.doesNotMatch(projectPreviewSource, /pp-env-empty|暂无环境变量/);
  assert.match(
    projectPreviewStyles,
    /\.pp-env-add\s*\{[\s\S]*?min-height:\s*52px;[\s\S]*?border:\s*1px dashed/,
  );
});

test("shows the total environment variable count beside the section title", () => {
  assert.match(
    projectPreviewSource,
    /const environmentVariableCount = automaticEnvRows\.length \+ envRows\.length;/,
  );
  assert.match(
    projectPreviewSource,
    /环境变量\s*<span className="pp-agent-child-count pp-env-count">\s*\{environmentVariableCount\} 项/,
  );
  assert.match(
    projectPreviewStyles,
    /\.pp-env-head \.pp-config-label\s*\{[\s\S]*?align-items:\s*center;[\s\S]*?gap:\s*7px;/,
  );
});

test("uses the builder typography hierarchy for deployment configuration", () => {
  assert.match(
    projectPreviewStyles,
    /\.pp-config-title\s*\{[\s\S]*?font-size:\s*17px;[\s\S]*?font-weight:\s*650;/,
  );
  assert.match(
    projectPreviewStyles,
    /\.pp-config-label\s*\{[\s\S]*?font-size:\s*15px;[\s\S]*?font-weight:\s*650;/,
  );
  assert.match(
    projectPreviewStyles,
    /\.pp-env-row input:first-child\s*\{[\s\S]*?font-family:\s*inherit;/,
  );
});

test("requires explicit confirmation before starting deployment", () => {
  const requestConfirmation = projectPreviewSource.slice(
    projectPreviewSource.indexOf(
      "async function requestDeploymentConfirmation",
    ),
    projectPreviewSource.indexOf("async function performDeployment"),
  );
  const performDeployment = projectPreviewSource.slice(
    projectPreviewSource.indexOf("async function performDeployment"),
    projectPreviewSource.indexOf("function cancelDeploymentConfirmation"),
  );

  assert.match(requestConfirmation, /setDeployConfirmOpen\(true\)/);
  assert.doesNotMatch(requestConfirmation, /await onDeploy/);
  assert.match(performDeployment, /await onDeploy/);
  assert.match(
    projectPreviewSource,
    /部署后暂不支持修改 Agent 配置，确定部署吗？/,
  );
  assert.match(projectPreviewSource, />\s*取消\s*</);
  assert.match(projectPreviewSource, />\s*确定部署\s*</);
});

test("moves completed deployment results into the global task card", () => {
  assert.match(projectPreviewSource, /endpoint: result\.url/);
  assert.match(projectPreviewSource, /consoleUrl: result\.consoleUrl/);
  assert.match(projectPreviewSource, /部署完成，下一步操作已保存到右上角部署任务/);
  assert.doesNotMatch(projectPreviewSource, /async function handleAddAgent/);
  assert.doesNotMatch(projectPreviewSource, /className="pp-deploy-result"/);
  assert.doesNotMatch(projectPreviewSource, /className="pp-deploy-complete"/);

  assert.match(appSource, /<dt>API 端点<\/dt>/);
  assert.match(appSource, /className="global-deploy-chat"/);
  assert.match(appSource, /className="global-deploy-console"/);
  assert.match(appSource, /label="复制端点"/);
  assert.match(appSource, /const openDeploymentTaskChat = async \(task: DeploymentTaskUpdate\)/);
  assert.match(appSource, /from "\.\/ui\/icons\/DeploymentTaskIcons"/);
  assert.doesNotMatch(appSource, /ExternalLink|MessageSquare/);
  assert.doesNotMatch(projectPreviewSource, /apiKey\?: string|apiKey: result\.apikey/);
  assert.match(appStyles, /\.global-deploy-meta-wide\s*\{/);
  assert.match(appStyles, /\.global-deploy-item-actions \.global-deploy-chat\s*\{/);
  assert.match(appStyles, /\.global-deploy-item-actions button:focus-visible/);
  assert.match(appStyles, /min-height:\s*28px/);
  assert.match(appSource, /ref=\{triggerRef\}/);
  assert.match(appSource, /autoOpenedSuccessIdRef/);
  assert.match(appSource, /latest\?\.status !== "success"/);
  assert.match(appSource, /setOpen\(true\)/);
  assert.match(appSource, /aria-modal="true"/);
  assert.match(appSource, /event\.key === "Escape"/);
  assert.match(appSource, /event\.key === "Tab"/);
  assert.match(appSource, /role="alert"/);
  assert.match(appSource, /复制失败，请手动选择 API 端点复制/);
  assert.match(deploymentTaskIconsSource, /export function DeployTaskChatIcon/);
  assert.match(deploymentTaskIconsSource, /stroke="currentColor"/);
});

test("renders deployment progress as a compact progress bar", () => {
  assert.match(projectPreviewSource, /className="pp-progress-track"/);
  assert.match(projectPreviewSource, /className=\{`pp-progress-line/);
  assert.match(projectPreviewSource, /className="pp-step-spinner"/);
  assert.match(projectPreviewSource, /className="pp-step-check"/);
  assert.match(projectPreviewSource, /className="pp-step-state"/);
  assert.match(
    projectPreviewStyles,
    /\.pp-progress-track\s*\{[\s\S]*?position:\s*relative;/,
  );
  assert.match(
    projectPreviewStyles,
    /\.pp-progress-line\s*\{[\s\S]*?position:\s*absolute;[\s\S]*?height:\s*4px;/,
  );
  assert.match(
    projectPreviewStyles,
    /\.pp-steps\s*\{[\s\S]*?grid-template-columns:\s*repeat\(3, minmax\(0, 1fr\)\);/,
  );
  assert.match(
    projectPreviewStyles,
    /\.pp-step-dot\s*\{[\s\S]*?width:\s*24px;[\s\S]*?height:\s*24px;/,
  );
  assert.match(
    projectPreviewStyles,
    /\.pp-step-spinner\s*\{[\s\S]*?width:\s*10px;[\s\S]*?height:\s*10px;/,
  );
  assert.match(
    projectPreviewStyles,
    /\.pp-step-check\s*\{[\s\S]*?border-left:\s*2px solid currentColor;/,
  );
  assert.match(
    projectPreviewStyles,
    /\.pp-step\.is-done \.pp-step-dot\s*\{[\s\S]*?background:\s*hsl\(142 45% 36%\);/,
  );
  assert.match(projectPreviewStyles, /@media \(prefers-reduced-motion: reduce\)/);
  assert.doesNotMatch(projectPreviewStyles, /\.pp-deploy-complete/);
});
