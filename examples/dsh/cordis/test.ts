


export type Dict<T> = Record<string, T>

interface demo { 
    a : Dict<string>
}

const newparam : symbol = Symbol('newparam')
const n1: Dict<Symbol> = {'abc': Symbol('abc')}

const testparam = {
    a : 'test_a',
    b : Symbol
}


console.log(testparam.a)
console.log(testparam.b)

const testparamb = {
    [testparam.a] : 'test',
    newparam : 'b'
}