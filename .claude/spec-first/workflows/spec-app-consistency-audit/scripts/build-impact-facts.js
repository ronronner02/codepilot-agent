#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');

const {
  buildAppAuditCoverageCapabilities,
  buildAppAuditInputExpectations,
  buildAppAuditVerdictScope,
  collectGitDiffFacts,
  evidence,
  includedUntrackedSourceFiles,
  listSourceTextFiles,
  makeArtifact,
  partialReadDegradedModes,
  parseCommonArgs,
  readTextWithMetadata,
  resolveRepoRoot,
  sourceInputFromFiles,
  toPosix,
  unique,
  unavailableSourceInput,
  writeJsonOutput,
} = require('./lib/audit-utils');

const INTERACTION_PATTERNS = [
  ['navigation', /navController|navigate\(|Navigation|Router|route|deeplink|DeepLink/i],
  ['form_submit', /submit|confirm|checkout|placeOrder|pay|commit|提交|确认|支付|下单/i],
  ['keyboard', /keyboard|ime|TextField|EditText|FocusRequester|焦点|键盘/i],
  ['dialog', /dialog|sheet|modal|alert|popup|弹窗|确认框/i],
  ['permission', /permission|authorization|camera|location|photo|权限/i],
  ['error_state', /error|failed|failure|exception|retry|错误|失败|重试/i],
  ['loading_state', /loading|progress|spinner|skeleton|加载/i],
  ['accessibility_risk', /accessibility|contentDescription|semantics|VoiceOver|TalkBack|aria|无障碍/i],
];

function buildImpactFacts(options = {}) {
  const repoRoot = resolveRepoRoot(options);
  if (options.mode === 'headless' && !options.base) {
    throw new Error('scope_headless_missing_base: mode:headless requires base:<ref> for deterministic diff scope.');
  }

  const source = listSourceTextFiles({
    repoRoot,
    source: options.source || '.',
    allowOutside: options.allowOutside,
    maxFiles: options.maxFiles || options.maxScanFiles || 2000,
  });
  const diffScope = collectDiffScope(repoRoot, options);
  const changedFiles = diffScope.changedFiles.length > 0 ? diffScope.changedFiles : [];
  const sourceScopedChangedFiles = filterChangedFilesBySource(repoRoot, source.sourceRoot, changedFiles, true);
  const outOfSourceChangedFiles = filterChangedFilesBySource(repoRoot, source.sourceRoot, changedFiles, false);
  const includedUntrackedFiles = includedUntrackedSourceFiles(repoRoot, source.files, diffScope.untrackedFiles);
  const signalFacts = buildCandidateSignals({
    repoRoot,
    sourceRoot: source.sourceRoot,
    changedFiles: sourceScopedChangedFiles,
    mode: options.mode || 'default',
    industry: options.industry || options.confirmedIndustry || null,
    confirmedIndustry: options.confirmedIndustry || null,
  });
  const candidateSignals = signalFacts.signals;
  const sourceInputs = [
    diffScope.sourceInput,
    sourceInputFromFiles('code', source.files, repoRoot, {
      sourceRoot: source.sourceRoot,
      truncated: source.truncated,
      maxFiles: source.maxFiles,
      skippedLargeFiles: source.skippedLargeFiles,
      skippedLargeFileCount: source.skippedLargeFileCount,
    }),
  ];

  return makeArtifact({
    schemaVersion: 'spec-app-consistency-audit-impact-facts.v1',
    artifactId: 'impact-facts',
    sourceInputs,
    consumers: ['llm-audit-planner', 'expert-agents', 'report-writer'],
    body: {
      mode: options.mode || 'default',
      base_ref: options.base || '',
      diff_scope: {
        kind: diffScope.kind,
        base_ref: options.base || '',
        changed_file_count: changedFiles.length,
        source_scoped_changed_file_count: sourceScopedChangedFiles.length,
        out_of_source_changed_file_count: outOfSourceChangedFiles.length,
        source_scoped_changed_files: sourceScopedChangedFiles,
        out_of_source_changed_files: outOfSourceChangedFiles,
        diff_hash: diffScope.diffHash,
        untracked_files: diffScope.untrackedFiles,
        untracked_policy: 'source_snapshot_includes_scanned_untracked',
        included_untracked_files: includedUntrackedFiles,
      },
      changed_files: changedFiles,
      candidate_signals: candidateSignals,
      interaction_surface_changed: candidateSignals.filter((signal) => signal.type === 'interaction_surface_changed'),
      available_context: {
        prd: Boolean(options.prd),
        figma_materialized: Boolean(options.figmaContext),
        figma_reference_only: Boolean(options.figmaRef || options.figmaNode || options.figmaFile) && !options.figmaContext,
        graph_artifacts: false,
        analytics: candidateSignals.some((signal) => signal.type === 'analytics_change'),
        i18n: candidateSignals.some((signal) => signal.type === 'i18n_change'),
      },
      coverage_capabilities: buildAppAuditCoverageCapabilities(options),
      input_expectations: buildAppAuditInputExpectations(options),
      audit_verdict_scope: buildAppAuditVerdictScope(options),
      degraded_modes: [
        ...source.degraded_modes,
        ...signalFacts.degraded_modes,
        ...buildInputDegradedModes(options),
      ],
    },
  });
}

function filterChangedFilesBySource(repoRoot, sourceRoot, changedFiles, insideSource) {
  const root = path.resolve(repoRoot || '.');
  const source = path.resolve(sourceRoot || root);
  return changedFiles.filter((filePath) => {
    const absolutePath = path.resolve(root, filePath);
    const isInside = isInsideOrSame(source, absolutePath);
    return insideSource ? isInside : !isInside;
  });
}

function collectDiffScope(repoRoot, options) {
  const diffFacts = collectGitDiffFacts(repoRoot, options);
  if (diffFacts.kind === 'git_diff') {
    return {
      kind: 'git_diff',
      changedFiles: diffFacts.changedFiles,
      diffHash: diffFacts.diffHash,
      untrackedFiles: diffFacts.untrackedFiles,
      sourceInput: {
        type: 'git_diff',
        path: '.',
        source_hash: diffFacts.diffHash,
        freshness: 'current-run',
        base_ref: options.base,
        resolved_base_sha: diffFacts.resolved_base_sha,
        effective_base_sha: diffFacts.effective_base_sha,
      },
    };
  }

  if (diffFacts.kind === 'working_tree') {
    return {
      kind: 'working_tree',
      changedFiles: diffFacts.changedFiles,
      diffHash: diffFacts.diffHash,
      untrackedFiles: diffFacts.untrackedFiles,
      sourceInput: {
        type: 'git_diff',
        path: '.',
        source_hash: diffFacts.diffHash,
        freshness: 'current-worktree',
      },
    };
  }

  return {
    kind: 'source_snapshot',
    changedFiles: [],
    diffHash: diffFacts.diffHash,
    untrackedFiles: [],
    sourceInput: unavailableSourceInput('git_diff', '.', 'not_a_git_repository'),
  };
}

function buildCandidateSignals(options) {
  const changedFiles = options.changedFiles || [];
  const signals = [];
  const reads = [];
  for (const filePath of changedFiles) {
    const absolutePath = path.join(options.repoRoot, filePath);
    const read = fs.existsSync(absolutePath) && fs.statSync(absolutePath).isFile()
      ? readTextWithMetadata(absolutePath, 64 * 1024)
      : null;
    if (read) reads.push(read);
    const text = read ? read.text : '';
    const summaryFile = toPosix(filePath);

    addPathSignal(signals, filePath, text, /Screen|View|Fragment|Activity|ViewController|Composable/i, 'screen_changed', 'Screen or view file changed.');
    addPathSignal(signals, filePath, text, /ViewModel|UiState|StateFlow|ObservableObject/i, 'viewmodel_state_changed', 'ViewModel or UI state file changed.');
    addPathSignal(signals, filePath, text, /UseCase|Interactor|Repository|Service/i, 'usecase_changed', 'Domain or use-case file changed.');
    addPathSignal(signals, filePath, text, /analytics|track|event|埋点/i, 'analytics_change', 'Analytics-related file or event code changed.');
    addPathSignal(signals, filePath, text, /strings\.xml|\.strings$|Localizable|i18n|l10n|StringResource/i, 'i18n_change', 'i18n resource or localized copy code changed.');
    addPathSignal(signals, filePath, text, /build\.gradle|settings\.gradle|Package\.swift|module|sourceSets/i, 'architecture_change', 'Module, build, or source-set file changed.');
    addPathSignal(signals, filePath, text, /component|designsystem|PrimaryButton|Theme|Style|Composable/i, 'component_change', 'Component or design-system code changed.');

    const interactionSubtypes = INTERACTION_PATTERNS
      .filter(([, pattern]) => pattern.test(filePath) || pattern.test(text))
      .map(([type]) => type);
    if (interactionSubtypes.length > 0) {
      signals.push({
        type: 'interaction_surface_changed',
        subtype: interactionSubtypes[0],
        subtypes: unique(interactionSubtypes),
        confidence: 0.74,
        affected_surface: {
          type: inferAffectedSurfaceType(filePath, text),
          id: inferSurfaceId(filePath, text),
          file: summaryFile,
        },
        platform: inferPlatforms(filePath, text),
        state_or_transition: summarizeInteraction(interactionSubtypes),
        evidence: [evidence('code', summaryFile, `Changed file contains ${interactionSubtypes.join(', ')} interaction signals.`)],
        static_confidence: 0.74,
        runtime_verification_hint: interactionSubtypes.includes('accessibility_risk') || interactionSubtypes.includes('keyboard')
          ? 'simulator: verify focus order, keyboard behavior and assistive announcement.'
          : 'simulator: verify changed interaction path.',
        coverage_cap: interactionSubtypes.some((type) => ['keyboard', 'accessibility_risk', 'permission'].includes(type))
          ? 'candidate_until_runtime_verified'
          : 'static_confirmed',
      });
    }

    const industrySignal = inferIndustrySignal(filePath, text, options.industry, options.confirmedIndustry);
    if (industrySignal) signals.push(industrySignal);
  }
  return {
    signals: dedupeSignals(signals),
    degraded_modes: partialReadDegradedModes(reads, options.repoRoot, {
      summary: 'Changed file exceeded impact signal read limit; candidate signals may be incomplete.',
    }),
  };
}

function addPathSignal(signals, filePath, text, pattern, type, summary) {
  if (!pattern.test(filePath) && !pattern.test(text)) return;
  signals.push({
    type,
    confidence: 0.8,
    evidence: [evidence('code', toPosix(filePath), summary)],
  });
}

function inferIndustrySignal(filePath, text, explicitIndustry, confirmedIndustry) {
  const haystack = `${filePath}\n${text}`.toLowerCase();
  const finance = /trade|order|stock|security|securities|broker|quote|buy|sell|支付|交易|订单|买入|卖出|委托|风控/.test(haystack);
  const industry = explicitIndustry || confirmedIndustry || null;
  if (!finance && !industry) return null;
  return {
    type: 'industry_term_candidate',
    industry: industry || 'finance-common',
    confidence: industry ? 0.85 : 0.68,
    advisory_only: confirmedIndustry !== (industry || 'finance-common'),
    evidence: [evidence('code', toPosix(filePath), 'Changed identifiers or text contain industry-sensitive terms.')],
  };
}

function inferAffectedSurfaceType(filePath, text) {
  if (/ViewModel|UiState/i.test(filePath) || /ViewModel|UiState/i.test(text)) return 'view_model';
  if (/route|nav|deeplink/i.test(filePath) || /navigate|route/i.test(text)) return 'route';
  if (/component|button|field/i.test(filePath)) return 'component';
  if (/permission/i.test(filePath) || /permission/i.test(text)) return 'permission';
  return /screen|view|fragment|activity/i.test(filePath) ? 'screen' : 'component';
}

function inferSurfaceId(filePath, text) {
  const classMatch = text.match(/\b(?:class|struct|object|fun)\s+([A-Z][A-Za-z0-9_]*)/);
  if (classMatch) return classMatch[1];
  return path.basename(filePath).replace(/\.[^.]+$/, '');
}

function inferPlatforms(filePath, text) {
  const values = [];
  if (/android|compose|kotlin|\.kt$/i.test(filePath) || /androidx|Composable/i.test(text)) values.push('android');
  if (/ios|swift|uikit|swiftui|\.swift$/i.test(filePath) || /SwiftUI|UIView/i.test(text)) values.push('ios');
  if (/commonMain|shared|kmp|kotlin/i.test(filePath)) values.push('kmp');
  return unique(values.length > 0 ? values : ['unknown']);
}

function summarizeInteraction(subtypes) {
  return `Changed interaction candidates: ${unique(subtypes).join(', ')}`;
}

function buildInputDegradedModes(options) {
  const expected = new Set(options.expectedInputs || []);
  const modes = [];
  if (expected.has('prd') && !options.prd) modes.push(mode('input_prd_missing', 'warning', 'Expected PRD input is missing.'));
  if (expected.has('figma_context') && !options.figmaContext) modes.push(mode('input_figma_context_missing', 'warning', 'Expected Figma context is missing.'));
  if (options.figmaRef && !options.figmaContext) modes.push(mode('input_figma_reference_only', 'warning', 'Figma reference is not a materialized context.'));
  if (expected.has('tech_plan') && !options.techPlan) modes.push(mode('input_tech_plan_missing', 'warning', 'Expected technical plan input is missing.'));
  if (expected.has('task_doc') && !options.taskDoc) modes.push(mode('input_task_doc_missing', 'warning', 'Expected task document input is missing.'));
  return modes;
}

function mode(code, severity, summary) {
  return { code, severity, summary, path: null };
}

function dedupeSignals(signals) {
  const seen = new Set();
  return signals.filter((signal) => {
    const file = signal.affected_surface ? signal.affected_surface.file : ((signal.evidence || [])[0] || {}).file;
    const key = `${signal.type}:${signal.subtype || ''}:${file || ''}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function isInsideOrSame(parentPath, childPath) {
  const relative = path.relative(parentPath, childPath);
  return relative === '' || (!relative.startsWith('..') && !path.isAbsolute(relative));
}

if (require.main === module) {
  let options = {};
  try {
    options = parseCommonArgs(process.argv.slice(2));
    const facts = buildImpactFacts(options);
    writeJsonOutput(facts, options.output, options);
  } catch (error) {
    if (options.mode === 'headless' && /^scope_/.test(error.message)) {
      const { renderHeadlessFailureEnvelope } = require('./render-headless-envelope');
      process.stdout.write(renderHeadlessFailureEnvelope({
        reasonCode: error.message.split(':')[0],
        message: error.message,
        runId: options.runId,
      }));
      process.exitCode = 1;
    } else {
      process.stderr.write(`${error.message}\n`);
      process.exitCode = 1;
    }
  }
}

module.exports = {
  buildImpactFacts,
};
