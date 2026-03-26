const fs = require('fs');
const path = require('path');
const vm = require('vm');

function extractFunction(source, name) {
    const start = source.indexOf(`function ${name}`);
    if (start === -1) {
        throw new Error(`Could not find function ${name}`);
    }
    let bodyStart = -1;
    let parenDepth = 0;
    let sawOpenParen = false;
    for (let i = start; i < source.length; i++) {
        const ch = source[i];
        if (ch === '(') {
            parenDepth += 1;
            sawOpenParen = true;
        } else if (ch === ')') {
            parenDepth -= 1;
        } else if (ch === '{' && sawOpenParen && parenDepth === 0) {
            bodyStart = i;
            break;
        }
    }
    if (bodyStart === -1) {
        throw new Error(`Could not find body for function ${name}`);
    }
    let braceDepth = 0;
    let end = -1;
    for (let i = bodyStart; i < source.length; i++) {
        const ch = source[i];
        if (ch === '{') {
            braceDepth += 1;
        } else if (ch === '}') {
            braceDepth -= 1;
            if (braceDepth === 0) {
                end = i + 1;
                break;
            }
        }
    }
    if (end === -1) {
        throw new Error(`Could not parse function ${name}`);
    }
    return source.slice(start, end);
}

const chatJs = fs.readFileSync(path.join(__dirname, '..', 'static', 'chat.js'), 'utf8');
const ingestSource = extractFunction(chatJs, 'ingestIncomingMessage');

let appendCalls = [];
const context = {
    mergeHistoryMessages(messages) {
        if (messages[0].id === 2) return false;
        return true;
    },
    appendMessage(msg, options) {
        appendCalls.push({ msg, options });
    },
};

const ingestIncomingMessage = vm.runInNewContext(`(${ingestSource})`, context);

function assert(condition, message) {
    if (!condition) throw new Error(message);
}

appendCalls = [];
let merged = ingestIncomingMessage({ id: 1, text: 'live' }, { renderLive: true });
assert(merged === true, 'expected live message merge to succeed');
assert(appendCalls.length === 1, 'expected live message to be appended');
assert(appendCalls[0].options && appendCalls[0].options.skipDuplicateCheck === true, 'expected live append to bypass duplicate guard');

appendCalls = [];
merged = ingestIncomingMessage({ id: 2, text: 'duplicate' }, { renderLive: true });
assert(merged === false, 'expected duplicate merge to report false');
assert(appendCalls.length === 0, 'expected duplicate live message not to append');

appendCalls = [];
merged = ingestIncomingMessage({ id: 3, text: 'history' }, { renderLive: false });
assert(merged === true, 'expected non-live merge to succeed');
assert(appendCalls.length === 0, 'expected non-live merge not to append');

console.log('test_bottom_up_chat_loading: PASS');
