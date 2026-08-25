#!/usr/bin/env node
'use strict';

const {
  evidence,
  listSourceTextFiles,
  makeArtifact,
  partialReadDegradedModes,
  parseCommonArgs,
  readTextWithMetadata,
  relativeTo,
  slugify,
  sourceInputFromFiles,
  toPosix,
  unique,
  writeJsonOutput,
} = require('./lib/audit-utils');

function extractCodeContract(options = {}) {
  const scan = listSourceTextFiles(options);
  const { repoRoot, files } = scan;
  const facts = files.map((filePath) => inspectSourceFile(filePath, repoRoot));
  const screens = extractScreens(facts);
  const routes = extractRoutes(facts);
  const viewModels = extractNamedFacts(facts, 'view_models');
  const uiStates = extractNamedFacts(facts, 'ui_states');
  const uiEvents = extractNamedFacts(facts, 'ui_events');
  const useCases = extractNamedFacts(facts, 'use_cases');
  const repositories = extractNamedFacts(facts, 'repositories');
  const components = extractNamedFacts(facts, 'components');
  const partialReadModes = partialReadDegradedModes(
    facts.map((fact) => fact.read),
    repoRoot,
  );

  return makeArtifact({
    schemaVersion: 'codebase-contract.v1',
    artifactId: 'codebase-contract',
    sourceInputs: [sourceInputFromFiles('code', files, repoRoot, scan)],
    body: {
      file_count: files.length,
      screens,
      routes,
      view_models: viewModels,
      ui_states: uiStates,
      ui_events: uiEvents,
      use_cases: useCases,
      repositories,
      components,
      platform_services: facts.flatMap((fact) => fact.platform_services),
      extraction_notes: [
        'Code contract is static candidate evidence; relationships are name/path based and require expert confirmation.',
      ],
      degraded_modes: [
        ...scan.degraded_modes,
        ...partialReadModes,
      ],
    },
  });
}

function inspectSourceFile(filePath, repoRoot) {
  const rel = relativeTo(repoRoot, filePath);
  const read = readTextWithMetadata(filePath);
  const text = read.text;
  const symbols = extractSymbols(text);
  const routeStrings = extractRouteStrings(text);
  const platformServices = [];
  if (/WebView|WKWebView/.test(text)) platformServices.push(platformFact('webview', rel));
  if (/Location|CLLocation|GPS/.test(text)) platformServices.push(platformFact('location', rel));
  if (/Keychain|Keystore|Biometric|Permission/.test(text)) platformServices.push(platformFact('security_or_permission', rel));

  return {
    file: rel,
    read,
    text,
    symbols,
    route_strings: routeStrings,
    screens: symbols.filter((name) => /(?:Screen|Page|Fragment|ViewController)$/.test(name)),
    view_models: symbols.filter((name) => /ViewModel$/.test(name)),
    ui_states: symbols.filter((name) => /UiState$|State$/.test(name)),
    ui_events: symbols.filter((name) => /UiEvent$|Event$/.test(name)),
    use_cases: symbols.filter((name) => /UseCase$|Interactor$/.test(name)),
    repositories: symbols.filter((name) => /Repository$/.test(name)),
    components: symbols.filter((name) => /(Button|Input|Card|Dialog|Sheet|Toast|Component)$/.test(name)),
    platform_services: platformServices,
  };
}

function extractSymbols(text) {
  const symbols = [];
  const patterns = [
    /\b(?:class|object|interface|enum class|sealed class|data class)\s+([A-Z][A-Za-z0-9_]*)/g,
    /\bfun\s+([A-Z][A-Za-z0-9_]*)\s*\(/g,
    /\bstruct\s+([A-Z][A-Za-z0-9_]*)/g,
    /\bfinal\s+class\s+([A-Z][A-Za-z0-9_]*)/g,
  ];
  for (const pattern of patterns) {
    let match = pattern.exec(text);
    while (match) {
      symbols.push(match[1]);
      match = pattern.exec(text);
    }
  }
  return unique(symbols);
}

function extractRouteStrings(text) {
  const routes = [];
  const patterns = [
    /(?:composable|route|navigation|navigate)\s*\(\s*"([^"]+)"/g,
    /(?:composable|route|navigation|navigate)\s*\(\s*'([^']+)'/g,
    /["']([a-z][a-z0-9_/-]*(?:\{[A-Za-z0-9_]+\})?[a-z0-9_}/-]*)["']/g,
  ];
  for (const pattern of patterns) {
    let match = pattern.exec(text);
    while (match) {
      const route = match[1];
      if (isRouteLike(route)) routes.push(route);
      match = pattern.exec(text);
    }
  }
  return unique(routes);
}

function isRouteLike(value) {
  return /\/|\{|\b(route|trade|order|profile|login|home|detail|checkout|cart)\b/i.test(value);
}

function extractScreens(facts) {
  return facts.flatMap((fact) => fact.screens.map((name) => ({
    id: slugify(name),
    name,
    file: fact.file,
    status: 'candidate',
    view_model: matchByPrefix(name, facts.flatMap((entry) => entry.view_models)),
    ui_state: matchByPrefix(name, facts.flatMap((entry) => entry.ui_states)),
    ui_events: matchManyByPrefix(name, facts.flatMap((entry) => entry.ui_events)),
    use_cases: matchManyByPrefix(name, facts.flatMap((entry) => entry.use_cases)),
    components: matchManyByText(fact.text, facts.flatMap((entry) => entry.components)),
    evidence: [evidence('code', fact.file, `Screen candidate detected from symbol ${name}`)],
  })));
}

function extractRoutes(facts) {
  const routes = [];
  for (const fact of facts) {
    for (const route of fact.route_strings) {
      routes.push({
        id: slugify(route),
        name: routeName(route),
        path: route,
        file: fact.file,
        screen: fact.screens[0] || null,
        status: 'candidate',
        required_params: unique((route.match(/\{([A-Za-z0-9_]+)\}/g) || []).map((param) => param.replace(/[{}]/g, ''))),
        guards: inferGuards(fact.text),
        evidence: [evidence('code', fact.file, `Route candidate detected: ${route}`)],
      });
    }
  }
  return dedupeRoutes(routes);
}

function extractNamedFacts(facts, field) {
  return facts.flatMap((fact) => fact[field].map((name) => ({
    id: slugify(name),
    name,
    file: fact.file,
    status: 'candidate',
    evidence: [evidence('code', fact.file, `Code symbol candidate: ${name}`)],
  })));
}

function routeName(route) {
  return route
    .split('/')
    .filter(Boolean)
    .map((part) => part.replace(/[{}]/g, ''))
    .map((part) => `${part.charAt(0).toUpperCase()}${part.slice(1)}`)
    .join('') || route;
}

function inferGuards(text) {
  const guards = [];
  if (/login|auth|authenticated|unauthorized|未登录/.test(text)) guards.push('login_required');
  if (/permission|denied|risk|风控|权限/.test(text)) guards.push('permission_or_risk_guard');
  return guards;
}

function matchByPrefix(name, candidates) {
  return matchManyByPrefix(name, candidates)[0] || null;
}

function matchManyByPrefix(name, candidates) {
  const prefix = name.replace(/(?:Screen|Page|Fragment|ViewController|Route)$/, '');
  return unique(candidates.filter((candidate) => candidate.startsWith(prefix)));
}

function matchManyByText(text, candidates) {
  return unique(candidates.filter((candidate) => text.includes(candidate)));
}

function platformFact(type, file) {
  return {
    type,
    file,
    status: 'candidate',
    evidence: [evidence('code', file, `Platform service usage candidate: ${type}`)],
  };
}

function dedupeRoutes(routes) {
  const seen = new Set();
  return routes.filter((route) => {
    const key = `${route.path}:${route.file}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

if (require.main === module) {
  try {
    const options = parseCommonArgs(process.argv.slice(2));
    writeJsonOutput(extractCodeContract(options), options.output, options);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = {
  extractCodeContract,
  inspectSourceFile,
};
