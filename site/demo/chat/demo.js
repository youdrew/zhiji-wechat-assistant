// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
'use strict';

// Every conversation, suggestion, and assistant reply is written for this demo.
const conversations = [
  {
    name: '林沐',
    messages: [
      { text: '这两天总在电脑前坐着，周末想出去透透气。' },
      { self: true, text: '你上次说河边那条路挺好走的，还想去吗？' },
      { text: '想呀，周六下午一起去河边走走？走累了还可以找家咖啡馆坐坐。' },
    ],
    analysis: '这是一次有具体时间和地点的邀请。她想在周末放松一下，回复时可以先确认有没有空，再把见面时间聊具体。',
    source: '她先提到“周末想出去透透气”，接着提出周六下午去河边走走。',
    suggestions: [
      '好呀，周六下午我有空。三点在桥头见？',
      '想去！我们沿河慢慢走，走累了就去喝杯咖啡。',
      '周六下午可以，我得四点以后才有空。你那个时间方便吗？',
    ],
    questions: [
      {
        text: '她是在正式约我吗？',
        answer: '她给了时间和地点，还问要不要一起去，这更像一次具体邀请。你可以先回应有没有空，再确认几点见。',
      },
      {
        text: '我想去，怎么回得自然一点？',
        answer: '先接住邀请，再补一句安排。例如：“好呀，三点在桥头见？咱们慢慢走，走累了就找家店坐坐。”这也接上了你们刚聊到的散步和咖啡。',
      },
    ],
  },
  {
    name: '苏禾',
    messages: [
      { text: '你上次推荐的摄影展我还没去，这周末好像是最后两天。' },
      { self: true, text: '我也记得快结束了，听说有一组街景很值得看。' },
      { text: '那周日下午去看展？如果你最近忙，改天一起吃饭也行。' },
    ],
    analysis: '她在邀请你看展，也主动给了另一种安排。展览快结束了，可以直接告诉她周日是否有空。',
    source: '“这周末好像是最后两天”说明看展有时间限制，她也提到可以改天吃饭。',
    suggestions: [
      '好呀，周日下午有空，我们在展馆门口见？',
      '我想看那组街景！下午两点过去怎么样？',
      '这周日不太行，咱们下周找天吃饭吧？',
    ],
    questions: [
      {
        text: '她更想看展还是约饭？',
        answer: '从这段聊天看，她先提出了看展，约饭是你忙时的备选。你可以直接选自己方便的一项。',
      },
      {
        text: '周日没空，怎么回？',
        answer: '简单说明周日没空，再接住她提的备选：“这周日不太行，不过我想一起吃饭。下周四晚上方便吗？”',
      },
    ],
  },
];

const byId = id => document.getElementById(id);
const sessionList = byId('session-list');
const messages = byId('messages');
const analysisToggle = byId('analysis-toggle');
const analysisPanel = byId('analysis-panel');
const suggestions = byId('suggestions');
const assistantToggle = byId('assistant-toggle');
const assistant = byId('assistant');
const assistantLog = byId('assistant-log');
const assistantQuestions = byId('assistant-questions');
const draft = byId('draft');
const draftStatus = byId('draft-status');

let analysisEnabled = true;
let analysisExpanded = true;
let analysisButton;

function node(tag, className, content) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (content) result.textContent = content;
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

function updateAnalysis() {
  analysisToggle.setAttribute('aria-checked', String(analysisEnabled));
  analysisPanel.hidden = !analysisEnabled || !analysisExpanded;
  analysisButton.hidden = !analysisEnabled;
  analysisButton.setAttribute('aria-expanded', String(!analysisPanel.hidden));
}

function setAssistant(open) {
  assistant.hidden = !open;
  assistantToggle.setAttribute('aria-expanded', String(open));
  byId('workspace').classList.toggle('assistant-open', open);
  if (open) byId('assistant-title').focus();
}

function renderMessages(conversation) {
  const rows = conversation.messages.map((item, index) => {
    const row = node('div', item.self ? 'message self' : 'message');
    row.append(avatar(item.self ? '我' : conversation.name[0]), node('p', 'bubble', item.text));
    if (index === conversation.messages.length - 1) {
      analysisButton = action('message-analysis-question', '✦', () => {
        analysisExpanded = !analysisExpanded;
        updateAnalysis();
      });
      analysisButton.setAttribute('aria-label', '查看这条消息的分析与建议');
      analysisButton.setAttribute('aria-controls', analysisPanel.id);
      row.append(analysisButton);
    }
    return row;
  });
  messages.replaceChildren(...rows);
}

function renderSuggestions(conversation) {
  suggestions.replaceChildren(...conversation.suggestions.map(text => {
    const card = node('article', 'message-analysis-candidate');
    card.append(
      node('p', 'message-analysis-candidate-text', text),
      action('tool-button', '试填草稿', () => {
        draft.value = text;
        draftStatus.textContent = '已填入演示草稿。';
        draft.focus();
      }),
    );
    return card;
  }));
}

function renderAssistant(conversation) {
  byId('assistant-context').textContent = '本轮上下文：@' + conversation.name;
  assistantLog.replaceChildren(node('p', 'welcome', '结合这段聊天，想聊聊哪一点？'));
  assistantQuestions.replaceChildren(...conversation.questions.map(question =>
    action('tool-button', question.text, () => {
      assistantLog.replaceChildren(node('p', 'question', question.text), node('p', 'answer', question.answer));
    }),
  ));
}

const sessionButtons = conversations.map((conversation, index) => {
  const button = action('session', '', () => selectConversation(index));
  const labels = node('span', 'session-text');
  labels.append(node('span', 'session-name', conversation.name),
    node('span', 'session-preview', conversation.messages.at(-1).text));
  button.setAttribute('aria-label', conversation.name);
  button.append(avatar(conversation.name[0]), labels);
  return button;
});

function selectConversation(index) {
  const conversation = conversations[index];
  sessionButtons.forEach((button, i) => button.setAttribute('aria-pressed', String(index === i)));
  byId('chat-title').textContent = conversation.name;
  byId('analysis-text').textContent = conversation.analysis;
  byId('analysis-source').textContent = conversation.source;
  analysisPanel.querySelector('details').open = false;
  analysisExpanded = true;
  draft.value = '';
  draftStatus.textContent = '点击一条建议试填草稿。';
  renderMessages(conversation);
  renderSuggestions(conversation);
  renderAssistant(conversation);
  updateAnalysis();
  messages.parentElement.scrollTop = 0;
}

analysisToggle.addEventListener('click', () => {
  analysisEnabled = !analysisEnabled;
  analysisExpanded = analysisEnabled;
  if (analysisEnabled && !assistant.hidden && matchMedia('(max-width: 860px)').matches) setAssistant(false);
  updateAnalysis();
});
assistantToggle.addEventListener('click', () => setAssistant(assistant.hidden));
byId('assistant-close').addEventListener('click', () => {
  setAssistant(false);
  assistantToggle.focus();
});
sessionList.append(...sessionButtons);
selectConversation(0);
