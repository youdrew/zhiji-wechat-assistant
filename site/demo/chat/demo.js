// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
'use strict';

// Fixed fictional conversations and prepared responses. Page state lives in memory.
const conversations = [
  {
    name: '林沐', topic: '周末散步', time: '18:42',
    title: '聊聊和林沐的周末安排',
    context: '刚结束设计项目，周末想放松。喜欢安静的路线和咖啡馆。',
    messages: [
      { time: '周五 18:20', text: '上次你说的那家山间咖啡馆，我终于去啦。窗边能看到整片树林。' },
      { self: true, text: '是吧，我当时就觉得你会喜欢。最近工作还忙吗？' },
      { text: '这周刚交完一个项目，终于可以慢下来一点了。' },
      { self: true, text: '辛苦了，周末给自己放个假吧。' },
      { text: '正有这个打算。周六想找个轻松的地方走走，你有空一起吗？' },
      { self: true, text: '我有空，不过这两天也有点累，不想把一天排得太满。' },
      { text: '那我们就沿河慢慢走，走累了找家店坐坐，不赶时间。' },
      { self: true, text: '听起来不错，我下午比较方便。' },
      { text: '周六三点在桥头见？如果下雨，直接去上次聊的那家咖啡馆也行。' },
    ],
    analyses: [
      { message: 4, text: '她从自己的近况聊到周末计划，再明确邀请你同行。可以先回应是否有空，把见面的时间和安排留给你们继续商量。', sources: [2, 4],
        suggestions: ['有空呀，我也想出去透透气。周六下午怎么样？', '想去！这次挑条轻松的路线，慢慢走就好。', '周六可以，不过我最近有点累，咱们别安排得太满。'] },
      { message: 8, text: '时间、地点和下雨时的备选都已经具体了。这条消息主要在等你确认，可以直接答应，顺便接住她留出的灵活安排。', sources: [5, 6, 8],
        suggestions: ['好呀，三点桥头见。下雨我们就去喝咖啡。', '可以，我三点前到！这次慢慢走，走累了再歇。', '好，先这样定。要是天气变了，我们上午再确认一下。'] },
    ],
    questions: [
      { text: '她是在正式约我吗？', answer: ['她已经提出周六三点在桥头见，还给了下雨的备选，这是一项具体的见面安排。你可以先确认自己是否方便，再补充需要调整的地方。'], quote: '好呀，三点桥头见。下雨我们就去喝咖啡。' },
      { text: '我想去，怎么回得自然一点？', answer: ['先接住邀请，再回应她提到的轻松安排。你们都说最近有点累，简单确认时间就已经足够。'], quote: '可以呀，三点见。咱们慢慢走，走累了就找家店坐坐。' },
      { text: '我怕自己显得不太积极', answer: ['她在你说明最近累之后，主动把安排改得更轻松。可以把“想见面”说在前面，再保留自己的节奏，不用反复解释。'], quote: '我挺期待的！三点见，刚好想和你聊聊近况。' },
      { text: '如果临时要加班怎么说？', answer: ['提前告诉她变化，同时给一个具体替代安排。这样她能尽早决定自己的时间。'], quote: '我这边临时有工作，三点可能赶不上。你看四点见，还是改到周日下午方便？' },
    ],
  },
  {
    name: '苏禾', topic: '摄影展与晚饭', time: '17:26',
    title: '怎么回应苏禾的看展邀请',
    context: '喜欢街景摄影，周末想看快闭展的摄影展。你周日下午有空。',
    messages: [
      { time: '周五 17:10', text: '你上次推荐的摄影展我还没去，这周末好像是最后两天。' },
      { self: true, text: '我也记得快结束了，听说有一组街景很值得看。' },
      { text: '我最近拍了不少路边小店，正想看看别人怎么拍日常。' },
      { self: true, text: '那你应该会喜欢，我记得还有一组夜里的公交站。' },
      { text: '周日下午一起去看展？如果你忙，改天一起吃饭也行。' },
      { self: true, text: '周日下午有空。你想几点过去？' },
      { text: '两点怎么样？看完展应该还有时间在附近吃点东西。' },
      { self: true, text: '可以，票要不要提前买？' },
      { text: '我来买吧，你看看附近有没有想吃的。我们到时慢慢逛。' },
    ],
    analyses: [
      { message: 4, text: '她先提看展，也为你忙的时候留了约饭这个备选。可以直接说周日是否方便，不需要同时回应两套安排。', sources: [0, 2, 4],
        suggestions: ['好呀，我周日下午有空。我们几点在展馆见？', '想看那组街景！下午两点过去怎么样？', '这周日不太行，不过想和你吃饭。下周四方便吗？'] },
      { message: 8, text: '她在承担买票这部分安排，并把选餐厅交给你。可以接住分工，给一两个方便选择的餐厅类型。', sources: [6, 7, 8],
        suggestions: ['好，那票辛苦你啦。我找两家附近的店，到时一起挑。', '可以呀，展馆旁边好像有家面馆，看完走过去吃？', '好，我看看附近有没有安静点的店，等会儿发你。'] },
    ],
    questions: [
      { text: '她更想看展还是约饭？', answer: ['她先提出看展，也解释了展览快结束。约饭是你忙时的备选。现在你们已经确认周日有空，可以围绕看展继续把行程说清楚。'] },
      { text: '选餐厅时要考虑什么？', answer: ['她说想慢慢逛，看完展的时间可以留一点弹性。挑展馆附近、步行可到的店，给两种选择就够了。'], quote: '我找了附近的面馆和一家小饭馆，咱们看完展按当时想吃的挑？' },
      { text: '怎么回应她帮忙买票？', answer: ['简单谢谢她，再接住她让你选餐厅的分工，话题就能自然往下走。'], quote: '好呀，票辛苦你啦。我负责找吃的，两点展馆门口见。' },
      { text: '周日临时没空怎么说？', answer: ['直接说明变化，尽早让她安排票和时间。她已经提过约饭，可以接住这个备选。'], quote: '周日临时有事，可能去不了了。还没买票的话先别买我的，我们下周找天吃饭好吗？' },
    ],
  },
  {
    name: '周屿', topic: '项目近况', time: '16:18',
    title: '听周屿聊聊最近的项目',
    context: '项目方案接连调整，周屿想先把问题讲清楚，希望得到具体反馈。',
    messages: [
      { time: '周五 15:50', text: '这周的方案又改了一版，我有点不知道先顾哪一头了。' },
      { self: true, text: '是目标变了，还是细节一直定不下来？' },
      { text: '主要是大家对用户需求的理解不一样，每次讨论都会多一项。' },
      { self: true, text: '那确实挺消耗的。你现在最想先定下来什么？' },
      { text: '先定核心流程吧。我想把新增的东西分到下一版，但还没想好怎么说。' },
      { self: true, text: '听起来你需要先把这一版要解决的问题收窄。' },
      { text: '嗯，今晚我把流程图整理出来。你明天有空帮我看看吗？' },
      { self: true, text: '明天下午可以。你想让我重点看哪里？' },
      { text: '帮我看看是不是能顺着走完，以及有哪些地方其实可以先不做。二十分钟就好。' },
    ],
    analyses: [
      { message: 4, text: '他已经找到了想先解决的部分，正在考虑怎样表达范围。可以先确认你对问题的理解，再问他是否需要一起整理说法。', sources: [2, 4],
        suggestions: ['先把核心流程跑通听起来挺合理的，要不要一起列一下这一版的范围？', '可以把新增项先记下来，等核心流程定了再排优先级。你想听听我的说法吗？', '听起来不是你没想清楚，而是需要大家先对齐这一版的目标。'] },
      { message: 8, text: '他明确说明了希望你检查的两件事，也给了时间预期。可以直接确认，并约定你会如何反馈。', sources: [6, 7, 8],
        suggestions: ['没问题，明天下午发我。我先顺着流程走一遍，再标出可以后放的部分。', '好，我明天下午三点左右看。看完给你几条具体反馈。', '可以，咱们先花二十分钟看核心路径，其他想法先记着。'] },
    ],
    questions: [
      { text: '他现在更需要安慰还是建议？', answer: ['前面他在讲反复修改带来的消耗，后面已经具体请求你检查流程。可以先简短接住他的感受，再确认你会怎样帮他看。'], quote: '这周来回改辛苦了。明天你发我，我先看能不能顺着走完，再帮你挑能后放的内容。' },
      { text: '怎么给反馈不显得指手画脚？', answer: ['按他提出的两个问题反馈，会比扩展成整体评价更容易讨论。先说你实际走到哪里卡住，再问他这一步原本想解决什么。'], quote: '我走到这里时不太确定下一步该选哪个。你原本希望用户先完成什么？' },
      { text: '我只能帮十分钟怎么说？', answer: ['早点说明自己的时间，给出你能实际完成的部分。可以先看主流程，再把细节留给他自己决定。'], quote: '我明天大概只有十分钟，可以先帮你走一遍主流程。细节的反馈可能要晚一点。' },
      { text: '怎样接住他最近的辛苦？', answer: ['用他已经说过的处境回应会更具体。比如提到目标一直增加、核心流程难定，而不是直接判断他做得好不好。'], quote: '一边改流程一边接新需求确实很累。先把这版的边界说清楚，可能会轻松一些。' },
    ],
  },
  {
    name: '陈夏', topic: '照片与分享', time: '14:08',
    title: '和陈夏商量照片怎么分享',
    context: '陈夏刚修好周末的照片，准备先让你挑选，再决定分享哪些。',
    messages: [
      { time: '周五 13:40', text: '周末那组照片修好了！光线比我想的还好。' },
      { self: true, text: '太好了，辛苦你。我很喜欢靠窗那几张。' },
      { text: '我也喜欢，选了六张，等会儿发你看看。' },
      { self: true, text: '好呀。有两张我好像眨眼了，可以先帮我挑掉。' },
      { text: '哈哈我避开了。还有一张你在低头看书，我想放到摄影小组里交流，可以吗？' },
      { self: true, text: '可以先让我看看是哪一张吗？' },
      { text: '当然，你挑过以后我再发。你不想公开的话我就自己留着。' },
      { self: true, text: '谢谢，先发我看看吧。' },
      { text: '发你啦。你选好告诉我，留一两张你喜欢的就行，其他我不放出去。' },
    ],
    analyses: [
      { message: 4, text: '她在分享照片之前先征求你的意见。可以直接表达自己的偏好，也可以先看图再决定，沿着她提出的确认步骤继续聊。', sources: [3, 4],
        suggestions: ['先发我看看吧，如果我也喜欢那张就可以。', '谢谢先问我！这次我想留着自己看，就先不公开啦。', '我先挑一张可以公开的给你，其他先替我留着。'] },
      { message: 8, text: '她接受了先给你挑选的安排，也明确表示只分享你同意的照片。可以告诉她你选了哪几张，并说清分享范围。', sources: [6, 7, 8],
        suggestions: ['我喜欢靠窗那张，可以放摄影小组。其他几张先替我留着，谢谢啦。', '我先慢慢挑，今晚告诉你选哪张。辛苦你修图了！', '这次想都留着自己看，就先不公开了。照片修得很好，谢谢你。'] },
    ],
    questions: [
      { text: '不想公开照片怎么说？', answer: ['她已经说过不想公开也没关系，可以直接说明自己的选择，再谢谢她先确认。'], quote: '谢谢你先问我。这组我想留着自己看，就先不公开啦。' },
      { text: '想夸她拍得好，怎么具体一点？', answer: ['你前面提到了靠窗的光线，可以接着说自己喜欢画面里的哪个细节，让感谢落在具体照片上。'], quote: '靠窗那张我很喜欢，光线很柔和，低头看书的样子也很自然。谢谢你把那个瞬间拍下来。' },
      { text: '我还没时间选，怎么回？', answer: ['给一个你能做到的时间就够了。她说可以慢慢挑，你可以先告诉她已经收到，再约定什么时候反馈。'], quote: '收到了！我今晚仔细看看，选好告诉你，先别急着发。' },
      { text: '怎样说清楚分享范围？', answer: ['把允许的照片和允许的地方说具体，方便她按你的意思操作。'], quote: '靠窗那张可以放摄影小组，其他几张先不公开。发之前给我看一下配文就好。' },
    ],
  },
];

const byId = id => document.getElementById(id);
const sessionList = byId('session-list');
const messages = byId('messages');
const analysisToggle = byId('analysis-toggle');
const analysisPanel = byId('analysis-panel');
const sourceDetails = analysisPanel.querySelector('details');
const assistantToggle = byId('assistant-toggle');
const assistant = byId('assistant');
const assistantLog = byId('assistant-log');
const assistantQuestions = byId('assistant-questions');
const assistantPrompt = byId('assistant-prompt');
const draft = byId('draft');
const draftStatus = byId('draft-status');
const states = conversations.map(conversation => ({
  enabled: true, expanded: true, analysis: conversation.analyses.length - 1,
  draft: '', question: 0, turns: [],
}));
let active = 0;
let analysisButtons = [];
let analysisTargets = [];
const current = () => conversations[active];
const currentState = () => states[active];

function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text) result.textContent = text;
  return result;
}
function action(className, label, onClick) {
  const button = node('button', className, label);
  button.type = 'button';
  button.addEventListener('click', onClick);
  return button;
}
function avatar(letter) {
  const result = node('span', 'avatar', letter);
  result.setAttribute('aria-hidden', 'true');
  return result;
}
function renderDraft() {
  draft.value = currentState().draft;
  draftStatus.textContent = draft.value ? '已填入演示草稿' : '点击一条建议试填草稿';
  byId('draft-clear').disabled = !draft.value;
}
function renderAnalysis() {
  const state = currentState(), conversation = current(), result = conversation.analyses[state.analysis];
  analysisToggle.setAttribute('aria-checked', String(state.enabled));
  analysisPanel.hidden = !state.enabled || !state.expanded;
  analysisButtons.forEach((button, index) => {
    button.hidden = !state.enabled;
    button.setAttribute('aria-expanded', String(state.enabled && state.expanded && state.analysis === index));
    analysisTargets[index].parentElement.classList.toggle('analysis-selected', state.analysis === index);
  });
  byId('analysis-text').textContent = result.text;
  byId('analysis-source').replaceChildren(...result.sources.map(index => {
    const item = conversation.messages[index];
    return node('p', 'source-quote', (item.self ? '我' : conversation.name) + '：' + item.text);
  }));
  byId('suggestions').replaceChildren(...result.suggestions.map((text, index) => {
    const card = node('article', 'message-analysis-candidate');
    const choose = action('message-analysis-copy', '试填草稿', () => {
      currentState().draft = text;
      renderDraft();
      draft.focus();
    });
    choose.setAttribute('aria-label', '将建议 ' + (index + 1) + ' 试填到演示草稿');
    card.append(node('p', 'message-analysis-candidate-text', text), choose);
    return card;
  }));
  analysisTargets[state.analysis].append(analysisPanel);
}
function selectAnalysis(index) {
  const state = currentState();
  state.expanded = state.analysis === index ? !state.expanded : true;
  state.analysis = index;
  sourceDetails.open = false;
  renderAnalysis();
}
function renderMessages() {
  const conversation = current(), rows = [];
  analysisButtons = []; analysisTargets = [];
  conversation.messages.forEach((item, messageIndex) => {
    if (item.time) rows.push(node('p', 'chat-time', item.time));
    const row = node('div', item.self ? 'message self' : 'message');
    const content = node('div', 'message-content');
    const line = node('div', 'bubble-line');
    line.append(node('p', 'bubble', item.text));
    const analysisIndex = conversation.analyses.findIndex(result => result.message === messageIndex);
    if (analysisIndex !== -1) {
      const button = action('message-analysis-question', '✦', () => selectAnalysis(analysisIndex));
      button.setAttribute('aria-label', '查看第 ' + (messageIndex + 1) + ' 条消息的分析与建议');
      button.setAttribute('aria-controls', analysisPanel.id);
      line.append(button);
      analysisButtons[analysisIndex] = button;
      analysisTargets[analysisIndex] = content;
    }
    content.append(line);
    row.append(avatar(item.self ? '我' : conversation.name[0]), content);
    rows.push(row);
  });
  messages.replaceChildren(...rows);
  renderAnalysis();
}

function assistantTurn(question) {
  const user = node('div', 'advisor-msg user');
  user.append(node('p', 'advisor-bubble', question.text));
  const context = node('details', 'advisor-message-context');
  const summary = node('summary', 'advisor-context-toggle', '本轮上下文： @' + current().name + ' ⌄');
  const detail = node('div', 'advisor-context-details');
  detail.append(node('strong', 'advisor-context-contact-name', current().name),
    node('p', 'advisor-context-contact', '已提供 · 聊天记录、好友资料'),
    node('p', 'advisor-context-contact', current().context));
  context.append(summary, detail); user.append(context);
  const answer = node('div', 'advisor-msg assistant'), bubble = node('div', 'advisor-bubble');
  question.answer.forEach(paragraph => bubble.append(node('p', '', paragraph)));
  if (question.quote) bubble.append(node('blockquote', '', question.quote));
  answer.append(bubble);
  return [user, answer];
}
function renderAssistantLog() {
  const conversation = current(), state = currentState();
  if (!state.turns.length) {
    const welcome = node('div', 'assistant-welcome');
    welcome.append(node('strong', '', '聊聊和' + conversation.name + '的相处'),
      node('p', '', '选择一个问题，结合这段聊天一起想想。'));
    assistantLog.replaceChildren(welcome);
  } else {
    assistantLog.replaceChildren(...state.turns.flatMap(index => assistantTurn(conversation.questions[index])));
  }
  assistantLog.scrollTop = assistantLog.scrollHeight;
}
function selectQuestion(index) {
  currentState().question = index;
  assistantPrompt.value = current().questions[index].text;
  [...assistantQuestions.children].forEach((button, number) =>
    button.setAttribute('aria-pressed', String(index === number)));
  byId('assistant-hint').textContent = '选择预设问题后发送';
}
function renderAssistant() {
  byId('assistant-title').textContent = current().title;
  byId('assistant-context').replaceChildren(node('span', 'advisor-participant advisor-primary-contact', '@' + current().name));
  assistantQuestions.replaceChildren(...current().questions.map((question, index) =>
    action('assistant-question', question.text, () => selectQuestion(index))));
  selectQuestion(currentState().question);
  renderAssistantLog();
}
function openAssistant() {
  renderAssistant();
  assistant.showModal();
  assistantToggle.setAttribute('aria-expanded', 'true');
  byId('assistant-title').setAttribute('tabindex', '-1');
  byId('assistant-title').focus();
}
function sendQuestion() {
  const state = currentState();
  state.turns.push(state.question);
  if (state.turns.length > 8) state.turns.shift();
  renderAssistantLog();
  byId('assistant-hint').textContent = '已展示预先准备的演示回答';
}

const sessionButtons = conversations.map((conversation, index) => {
  const button = action('session', '', () => selectConversation(index));
  const labels = node('span', 'session-text'), top = node('span', 'session-top');
  top.append(node('span', 'session-name', conversation.name), node('span', 'session-time', conversation.time));
  labels.append(top, node('span', 'session-preview', conversation.messages.at(-1).text));
  button.setAttribute('aria-label', conversation.name);
  button.append(avatar(conversation.name[0]), labels);
  return button;
});
function selectConversation(index) {
  active = index;
  sessionButtons.forEach((button, number) => button.setAttribute('aria-pressed', String(index === number)));
  byId('chat-title').textContent = current().name;
  byId('chat-topic').textContent = current().topic;
  sourceDetails.open = false;
  renderMessages();
  renderDraft();
  messages.parentElement.scrollTop = messages.parentElement.scrollHeight;
}

analysisToggle.addEventListener('click', () => {
  const state = currentState();
  state.enabled = !state.enabled;
  state.expanded = state.enabled;
  renderAnalysis();
});
byId('session-search').addEventListener('input', event => {
  const query = event.target.value.trim();
  sessionButtons.forEach((button, index) => { button.hidden = !conversations[index].name.includes(query); });
  byId('session-empty').hidden = sessionButtons.some(button => !button.hidden);
});
byId('draft-clear').addEventListener('click', () => {
  currentState().draft = '';
  renderDraft();
});
assistantToggle.addEventListener('click', openAssistant);
byId('assistant-close').addEventListener('click', () => assistant.close());
assistant.addEventListener('close', () => {
  assistantToggle.setAttribute('aria-expanded', 'false');
  assistantToggle.focus();
});
byId('assistant-new').addEventListener('click', () => {
  currentState().turns = [];
  renderAssistantLog();
  selectQuestion(0);
});
byId('assistant-send').addEventListener('click', sendQuestion);
assistantPrompt.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    sendQuestion();
  }
});
sessionList.append(...sessionButtons);
selectConversation(0);
