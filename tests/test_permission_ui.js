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

function assert(condition, message) {
    if (!condition) throw new Error(message);
}

const chatJs = fs.readFileSync(path.join(__dirname, '..', 'static', 'chat.js'), 'utf8');
const helperSource = extractFunction(chatJs, 'getPermissionOptionAction');
const getPermissionOptionAction = vm.runInNewContext(`(${helperSource})`);

assert(getPermissionOptionAction({ label: 'Apply', key: 'y' }) === 'approve', 'expected Apply to approve');
assert(getPermissionOptionAction({ label: 'Skip', key: 'a' }) === 'deny', 'expected Skip to deny');
assert(getPermissionOptionAction({ label: 'Cancel', key: 'esc' }) === 'deny', 'expected esc to deny');
assert(getPermissionOptionAction({ label: 'Allow once', key: '1' }) === 'approve', 'expected Allow once to approve');
assert(getPermissionOptionAction({ label: 'No', key: '2' }) === 'deny', 'expected No to deny');

console.log('test_permission_ui: PASS');
