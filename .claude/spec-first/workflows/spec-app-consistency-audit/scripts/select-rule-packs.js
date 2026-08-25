#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');

const {
  evidence,
  makeArtifact,
  parseCommonArgs,
  readArtifact,
  sourceInputFromFile,
  unavailableSourceInput,
  writeJsonOutput,
} = require('./lib/audit-utils');

function selectRulePacks(options = {}) {
  const preflight = options.preflight ? readArtifact(options.preflight) : null;
  const industryProfile = options.industryProfile ? readArtifact(options.industryProfile) : null;
  const explicitIndustry = options.industry || options.confirmedIndustry || null;
  const confirmedIndustry = options.confirmedIndustry || null;
  const selected = [];
  const degradedModes = [];

  selected.push(rulePack('common-app', 'common', false, 'Common mobile app review baseline is always useful.'));
  if (!preflight || (preflight.architecture_candidates || []).includes('kmp')) {
    selected.push(rulePack('kmp-clean-architecture', 'architecture', false, 'KMP or architecture candidate is present.'));
  }
  if (!preflight || preflight.has_component_system || preflight.has_modular_structure) {
    selected.push(rulePack('component-module-reuse', 'architecture', false, 'Component or module structure signal is present.'));
  }
  selected.push(rulePack(
    'analytics',
    'quality',
    false,
    !preflight || preflight.has_analytics
      ? 'Analytics signal is present; review should check event consistency on critical journeys.'
      : 'Analytics signal is missing; review should check whether this is acceptable for App critical journeys.',
  ));
  selected.push(rulePack(
    'i18n',
    'quality',
    false,
    !preflight || preflight.has_i18n
      ? 'I18n resources are present; review should check copy and key consistency.'
      : 'I18n resources are missing; review should check hardcoded text and product language scope.',
  ));

  const industryCandidates = industryProfile ? industryProfile.industry_candidates || [] : [];
  const previewIndustry = industryCandidates[0] ? industryCandidates[0].industry : null;
  const industry = explicitIndustry || previewIndustry;
  if (industry) {
    const confirmed = confirmedIndustry === industry;
    if (rulePackPath(industry, 'industry')) {
      selected.push(rulePack(
        industry,
        'industry',
        !confirmed,
        confirmed
          ? `Industry ${industry} was confirmed by caller-provided profile.`
          : (options.industry === industry
            ? `Industry ${industry} was explicitly selected as a lens and remains advisory-only until confirmed.`
            : `Industry ${industry} came from preview profile and remains advisory-only.`),
        industryCandidates.find((candidate) => candidate.industry === industry),
      ));
    } else {
      degradedModes.push({
        code: 'unknown_industry_rule_pack',
        severity: 'warning',
        summary: `Industry rule pack is not available: ${industry}`,
        path: null,
      });
    }
  }

  return makeArtifact({
    schemaVersion: 'rule-pack-selection.v1',
    artifactId: 'rule-pack-selection',
    sourceInputs: [
      ...(preflight && options.preflight ? [sourceInputFromFile('preflight', options.preflight, options.repoRoot || process.cwd())] : []),
      ...(industryProfile && options.industryProfile ? [sourceInputFromFile('industry-profile', options.industryProfile, options.repoRoot || process.cwd())] : []),
      ...(!preflight && !industryProfile ? [unavailableSourceInput('artifacts', 'preflight/industry-profile', 'selection_inputs_missing')] : []),
    ],
    body: {
      selected_rule_packs: selected,
      preview_only: true,
      confirmed_issue_policy: {
        rule_pack_cannot_be_only_evidence: true,
        project_specific_evidence_required: true,
      },
      extraction_notes: [
        'Rule packs provide rationale context only. Industry packs stay advisory-only until explicitly specified or confirmed.',
      ],
      degraded_modes: degradedModes,
    },
  });
}

function rulePack(name, layer, advisoryOnly, reason, industryCandidate = null) {
  return {
    name,
    path: rulePackPath(name, layer),
    layer,
    advisory_only: advisoryOnly,
    activation_reason: reason,
    evidence_requirements: [
      'At least one project-specific evidence source is required for a confirmed issue.',
      'Rule-pack rationale may appear in related_rule_packs or rationale, not as the only evidence.',
    ],
    evidence: industryCandidate && industryCandidate.evidence
      ? industryCandidate.evidence
      : [evidence('rule_pack_selection', null, reason)],
    status: 'candidate',
  };
}

function rulePackPath(name, layer) {
  if (!/^[a-z0-9][a-z0-9-]*$/i.test(String(name || ''))) return null;
  const relativeRoot = layer === 'industry'
    ? 'skills/spec-app-consistency-audit/rule-packs/industries'
    : 'skills/spec-app-consistency-audit/rule-packs';
  const root = path.resolve(__dirname, '..', relativeRoot.replace('skills/spec-app-consistency-audit/', ''));
  const candidate = path.resolve(root, name, 'rules.yaml');
  if (!fs.existsSync(candidate)) return null;
  const realRoot = fs.realpathSync(root);
  const realCandidateDir = fs.realpathSync(path.dirname(candidate));
  const relative = path.relative(realRoot, realCandidateDir);
  if (relative.startsWith('..') || path.isAbsolute(relative)) return null;
  return `${relativeRoot}/${name}/rules.yaml`;
}

if (require.main === module) {
  try {
    const options = parseCommonArgs(process.argv.slice(2));
    writeJsonOutput(selectRulePacks(options), options.output, options);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = {
  selectRulePacks,
};
