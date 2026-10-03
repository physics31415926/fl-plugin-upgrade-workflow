// Run: node --test tests/test_session_event_ui.cjs
// Exercise the actual renderer with anonymized app-server event shapes.
// No browser, network, server, vendor code or runtime evidence is loaded.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const repository = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(repository, 'sessions.html'), 'utf8');
const source = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)][0][1];
const sourceBetween = (start, end) => {
  const first = source.indexOf(start), last = source.indexOf(end, first);
  assert(first >= 0 && last > first, `renderer source exists: ${start}`);
  return source.slice(first, last);
};

class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this._text = '';
    this.scrollHeight = 500; this.scrollTop = 100; this.clientHeight = 100;
    this.replacements = 0;
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  append(...items) {
    for (const item of items) {
      if (item.tag === '#fragment') this.children.push(...item.children);
      else this.children.push(item);
    }
  }
  replaceChildren(...items) { this.children = []; this._text = ''; this.append(...items); this.replacements++; }
}

function renderer() {
  const nodes = new Map(['events','event-count','older-events','history-note','history-events','history-count'].map(id => [id, new Element('div')]));
  const context = {
    state: {eventsKey: '', policies: {}},
    API: '/api/codex-sessions', URLSearchParams, encodeURIComponent,
    document: {createElement: tag => new Element(tag), createDocumentFragment: () => new Element('#fragment')},
    $: id => nodes.get(id), formatTime: value => value || '—',
    errorMessage: value => typeof value === 'string' ? value : value.message || '本轮异常'
  };
  vm.createContext(context);
  vm.runInContext([
    sourceBetween('  function el(', '  function notice('),
    sourceBetween('  function taskPolicy(', '  function prioritizedSessions('),
    sourceBetween('  function eventOrigin(', '  async function respond(')
  ].join('\n'), context, {filename: 'sessions.html event renderer'});
  return {context, box: nodes.get('events'), count: nodes.get('event-count'), nodes};
}

const rootThread = 'root-thread-0001';
const childThread = 'child-alpha-0001';
const event = (scope, thread, method = 'turn/completed', params = {turn: {id: 'turn-example', status: 'completed'}}) => ({
  at: '2026-01-01T12:00:00Z', source_scope: scope, source_thread_id: thread, method, params
});
const session = (events, mode = 'adapt') => ({
  id: 'session-example', backend: 'nvidia', branch: 'baseline-example', status: 'running',
  thread_id: rootThread, events, task_policy: {mode, branch: 'baseline-example'},
  plan: [{step: 'Collect evidence', status: 'in_progress'}], gate: {status: 'missing'}
});
const visible = row => row.children.filter(child => child.tag !== 'details').map(child => child.textContent).join('\n');

test('older event pages keep command identity and acceptance separate while live events update', async () => {
  const {context,nodes,box} = renderer();
  const input=session([event('root',rootThread,'item/agentMessage/delta',{delta:'live'})]);
  context.current=()=>input;
  const completed=event('root',rootThread,'item/completed',{item:{id:'command-example',type:'commandExecution',status:'completed',exitCode:0,command:'read-only example'}});
  const calls=[];
  context.request=async url=>{calls.push(url);return {session_id:input.id,events:[completed],status:'ok',has_more:true,next_cursor:'opaque-cursor',warnings:[]};};
  const before=JSON.stringify(input);
  context.renderEvents(input);
  await context.loadOlderEvents();
  assert.equal(JSON.stringify(input),before);
  assert.match(calls[0], /\/session-example\/events\?limit=80$/);
  const history=nodes.get('history-events');
  assert.match(visible(history.children[0]), /OS 退出码：0（不代表测试或 Gate 结论）/);
  assert.match(history.children[0].children.at(-1).textContent,/command-example/);
  input.events=[event('root',rootThread,'item/agentMessage/delta',{delta:'new live'})];
  context.renderEvents(input);
  assert.match(box.textContent,/new live/);
  assert.match(history.textContent,/command-example/);
  await context.loadOlderEvents();
  assert.match(calls[1],/cursor=opaque-cursor/);
});

test('a late history response cannot cross session selection and unavailable history can retry', async () => {
  const {context,nodes}=renderer();
  const original=session([]),other={...session([]),id:'other-session'};
  let selected=original,complete;
  context.current=()=>selected;
  context.request=()=>new Promise(resolve=>{complete=resolve;});
  const pending=context.loadOlderEvents();
  assert.equal(nodes.get('older-events').disabled,true);
  selected=other;context.renderEventHistory(other);
  complete({session_id:original.id,events:[event('root',rootThread)],status:'ok',has_more:false});
  await pending;
  assert.equal(context.state.eventHistory.sessionId,other.id);
  assert.equal(context.state.eventHistory.page,null);
  assert.equal(nodes.get('older-events').disabled,false);
  context.request=async()=>({session_id:other.id,events:[],status:'unavailable',warnings:['history_missing'],has_more:false});
  await context.loadOlderEvents();
  assert.equal(nodes.get('older-events').disabled,false);
  assert.match(nodes.get('history-note').textContent,/暂不可用/);
});

test('history corruption and ended pagination are visible without offering task actions', async () => {
  const {context,nodes}=renderer(),input=session([],'monitor');
  context.current=()=>input;
  context.request=async()=>({session_id:input.id,events:[event('root',rootThread)],status:'partial',warnings:['corrupt_records'],has_more:false});
  await context.loadOlderEvents();
  assert.equal(nodes.get('older-events').disabled,true);
  assert.match(nodes.get('history-note').textContent,/损坏、不完整或超出读取上限/);
  assert.doesNotMatch(nodes.get('history-events').textContent,/继续此任务|下一轮指令/);
});

test('both inline page scripts compile without executing their startup code', () => {
  for (const name of ['index.html', 'sessions.html']) {
    const text = fs.readFileSync(path.join(repository, name), 'utf8');
    for (const match of text.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1], {filename: name});
  }
});

test('homepage requests compact cards and pauses automatic polling while hidden', () => {
  const page = fs.readFileSync(path.join(repository, 'index.html'), 'utf8');
  const script = [...page.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)][0][1].replace(/\r\n/g, '\n');
  assert.match(script, /fetch\(appUrl\("api\/codex-sessions\?view=cards"\)/);
  assert.doesNotMatch(script, /session\.(?:events|prompt)\b/);

  const start = script.indexOf('    setInterval(() => { if (!document.hidden) loadBranch');
  const end = script.indexOf('\n  }\n  initialize();', start);
  assert(start >= 0 && end > start, 'homepage polling source exists');
  const timers = new Map(), listeners = new Map(), calls = [];
  let hidden = true;
  vm.runInNewContext(script.slice(start, end), {
    document: {
      get hidden() { return hidden; },
      addEventListener(name, callback) { listeners.set(name, callback); }
    },
    setInterval(callback, milliseconds) { timers.set(milliseconds, callback); },
    sessionsEnabled: true,
    initialBranch: 'initial-branch',
    byId() { return {value: 'selected-branch'}; },
    loadBranch(branch) { calls.push(['branch', branch]); },
    loadTasks() { calls.push(['tasks']); }
  }, {filename: 'index.html polling'});
  assert.equal(timers.size, 2);
  timers.get(15000)(); timers.get(3000)();
  listeners.get('visibilitychange')();
  assert.deepEqual(calls, [], 'hidden tab does not poll');
  hidden = false;
  listeners.get('visibilitychange')();
  assert.deepEqual(calls, [['branch', 'selected-branch'], ['tasks']], 'visible tab refreshes immediately');
  timers.get(15000)(); timers.get(3000)();
  assert.deepEqual(calls.slice(2), [['branch', 'selected-branch'], ['tasks']], 'visible tab keeps polling');
});

test('parallel root and child completion remain distinct and preserve input state and raw event', () => {
  const {context, box} = renderer();
  const input = session([event('child', childThread), event('root', rootThread)]);
  const before = JSON.stringify(input);
  context.renderEvents(input);
  assert.equal(box.children.length, 2);
  assert.match(visible(box.children[0]), /子任务本轮结束/);
  assert.match(visible(box.children[0]), /子任务 · child-alpha-/);
  assert.equal(box.children[0].children[0].children[1].title, childThread);
  assert.doesNotMatch(visible(box.children[0]), /发送下一步指令|继续主任务/);
  assert.match(visible(box.children[1]), /主任务本轮结束/);
  assert.match(visible(box.children[1]), /主任务 · root-thread-/);
  assert.match(visible(box.children[1]), /发送下一步指令/);
  assert.equal(box.children[0].children.at(-1).children.at(-1).textContent, JSON.stringify(input.events[0], null, 2));
  assert.equal(JSON.stringify(input), before, 'rendering cannot change lifecycle, plan, Gate or events');
});

test('monitor policy rerenders unchanged events without a prompt to continue', () => {
  const {context, box} = renderer();
  const input = session([event('root', rootThread), event('child', childThread)]);
  context.renderEvents(input);
  assert.match(visible(box.children[0]), /发送下一步指令/);
  const rendered = box.replacements;
  input.task_policy.mode = 'monitor';
  context.renderEvents(input);
  assert.equal(box.replacements, rendered + 1, 'policy change invalidates the event render cache');
  assert.match(visible(box.children[0]), /仅监控已有 PR/);
  for (const row of box.children) assert.doesNotMatch(visible(row), /发送下一步指令|可继续|点击「启动任务」/);
  context.renderEvents(input);
  assert.equal(box.replacements, rendered + 1, 'unchanged polling preserves existing event nodes');
});

test('unknown source is not inferred to be the root from a thread id', () => {
  const {context, box} = renderer();
  context.renderEvents(session([event(undefined, rootThread), event('future-scope', undefined)]));
  for (const row of box.children) {
    assert.match(visible(row), /未知来源/);
    assert.match(visible(row), /来源未确认的本轮结束/);
    assert.doesNotMatch(visible(row), /发送下一步指令|主任务本轮结束/);
  }
  assert.match(visible(box.children[1]), /thread 未记录/);
});

test('delta grouping respects source and full thread id while preserving same-thread grouping', () => {
  const {context, box, count} = renderer();
  const delta = (scope, thread, text) => event(scope, thread, 'item/agentMessage/delta', {itemId: 'shared-item-id', delta: text});
  // Deliberately share both the item id and the displayed short thread prefix.
  const input = session([
    delta('root', rootThread, 'root'),
    delta('child', rootThread, 'different scope'),
    delta('child', 'child-shared-prefix-a', 'alpha'),
    delta('child', 'child-shared-prefix-b', 'beta'),
    delta('child', 'child-shared-prefix-b', '+continued'),
    delta(undefined, undefined, 'unknown one'),
    delta(undefined, undefined, 'unknown two')
  ]);
  context.renderEvents(input);
  assert.equal(box.children.length, 6);
  assert.match(visible(box.children[0]), /root/);
  assert.match(visible(box.children[1]), /different scope/);
  assert.match(visible(box.children[2]), /alpha/);
  assert.match(visible(box.children[3]), /beta\+continued/);
  assert.match(visible(box.children[4]), /unknown one/);
  assert.match(visible(box.children[5]), /unknown two/);
  assert.match(box.children[3].children.at(-1).children[0].textContent, /已合并 2 段输出/);
  assert.equal(count.textContent, '最近 7 条事件');
});

test('an empty prepared monitor task never asks the user to start it', () => {
  const {context, box} = renderer();
  const input = session([], 'monitor'); input.status = 'prepared';
  context.renderEvents(input);
  assert.match(box.textContent, /仅监控已有 PR/);
  assert.doesNotMatch(box.textContent, /点击「启动任务」/);
});
