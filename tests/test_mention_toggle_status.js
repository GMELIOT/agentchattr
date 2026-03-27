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

function createFakeElement(initialClasses = []) {
    const set = new Set(initialClasses);
    return {
        classList: {
            add: (...classes) => classes.forEach((cls) => set.add(cls)),
            remove: (...classes) => classes.forEach((cls) => set.delete(cls)),
            contains: (cls) => set.has(cls),
        },
        style: {
            values: {},
            setProperty(key, value) {
                this.values[key] = value;
            },
        },
        hasClass(cls) {
            return set.has(cls);
        },
    };
}

function assert(condition, message) {
    if (!condition) throw new Error(message);
}

const chatJs = fs.readFileSync(path.join(__dirname, '..', 'static', 'chat.js'), 'utf8');
const helperSource = extractFunction(chatJs, 'applyAgentStatusClasses');
const applyAgentStatusClasses = vm.runInNewContext(`(${helperSource})`);

const pendingPill = createFakeElement(['pending']);
applyAgentStatusClasses(pendingPill, { busy: true, available: true, color: '#10a37f' }, { preservePending: true });
assert(!pendingPill.hasClass('working'), 'pending pill should not lose its dedicated state');
assert(pendingPill.style.values['--agent-color'] === '#10a37f', 'pending pill should still sync agent color');

const mentionToggle = createFakeElement(['active']);
applyAgentStatusClasses(mentionToggle, { busy: true, available: true, color: '#10a37f' });
assert(mentionToggle.hasClass('active'), 'mention toggle should preserve active selection state');
assert(mentionToggle.hasClass('working'), 'mention toggle should gain working state');
assert(mentionToggle.style.values['--agent-color'] === '#10a37f', 'mention toggle should sync agent color');

const offlineToggle = createFakeElement();
applyAgentStatusClasses(offlineToggle, { busy: false, available: false });
assert(offlineToggle.hasClass('offline'), 'offline toggle should gain offline state');

console.log('test_mention_toggle_status: PASS');
