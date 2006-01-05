#!/usr/bin/env python
# $Id: Parser.py,v 1.93 2006/01/05 06:45:51 tavis_rudd Exp $
"""Parser classes for Cheetah's Compiler

Classes:
  ParseError( Exception )
  _LowLevelParser( Cheetah.SourceReader.SourceReader ), basically a lexer
  _HighLevelParser( _LowLevelParser )
  Parser === _HighLevelParser (an alias)

Meta-Data
================================================================================
Author: Tavis Rudd <tavis@damnsimple.com>
Version: $Revision: 1.93 $
Start Date: 2001/08/01
Last Revision Date: $Date: 2006/01/05 06:45:51 $
"""
__author__ = "Tavis Rudd <tavis@damnsimple.com>"
__revision__ = "$Revision: 1.93 $"[11:-2]

import os
import sys
import re
from re import DOTALL, MULTILINE
from types import StringType, ListType, TupleType
import time
from tokenize import pseudoprog
import inspect

from Cheetah.SourceReader import SourceReader
from Cheetah import Filters
from Cheetah import ErrorCatchers

# re tools
def escapeRegexChars(txt,
                     escapeRE=re.compile(r'([\$\^\*\+\.\?\{\}\[\]\(\)\|\\])')):
    
    """Return a txt with all special regular expressions chars escaped."""
    
    return escapeRE.sub(r'\\\1' , txt)

def group(*choices): return '(' + '|'.join(choices) + ')'
def nongroup(*choices): return '(?:' + '|'.join(choices) + ')'
def namedGroup(name, *choices): return '(P:<' + name +'>' + '|'.join(choices) + ')'
def any(*choices): return apply(group, choices) + '*'
def maybe(*choices): return apply(group, choices) + '?'

##################################################
## CONSTANTS & GLOBALS ##

NO_CACHE = 0
STATIC_CACHE = 1
REFRESH_CACHE = 2

SET_LOCAL = 0
SET_GLOBAL = 1
SET_MODULE = 2

##################################################
## Tokens for the parser ##

#generic
identchars = "abcdefghijklmnopqrstuvwxyz" \
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ_"
namechars = identchars + "0123456789"

#operators
powerOp = '**'
unaryArithOps = ('+', '-', '~')
binaryArithOps = ('+', '-', '/', '//','%')
shiftOps = ('>>','<<')
bitwiseOps = ('&','|','^')
assignOp = '='
augAssignOps = ('+=','-=','/=','*=', '**=','^=','%=',
          '>>=','<<=','&=','|=', )
assignmentOps = (assignOp,) + augAssignOps

compOps = ('<','>','==','!=','<=','>=', '<>', 'is', 'in',)
booleanOps = ('and','or','not')
operators = (powerOp,) + unaryArithOps + binaryArithOps \
            + shiftOps + bitwiseOps + assignmentOps \
            + compOps + booleanOps

delimeters = ('(',')','{','}','[',']',
              ',','.',':',';','=','`') + augAssignOps


keywords = ('and',       'del',       'for',       'is',        'raise',
            'assert',    'elif',      'from',      'lambda',    'return',
            'break',     'else',      'global',    'not',       'try',   
            'class',     'except',    'if',        'or',        'while',
            'continue',  'exec',      'import',    'pass',
            'def',       'finally',   'in',        'print',
            )

single3 = "'''"
double3 = '"""'

tripleQuotedStringStarts =  ("'''", '"""', 
                             "r'''", 'r"""', "R'''", 'R"""',
                             "u'''", 'u"""', "U'''", 'U"""',
                             "ur'''", 'ur"""', "Ur'''", 'Ur"""',
                             "uR'''", 'uR"""', "UR'''", 'UR"""')

tripleQuotedStringPairs = {"'''": single3, '"""': double3,
                           "r'''": single3, 'r"""': double3,
                           "u'''": single3, 'u"""': double3,
                           "ur'''": single3, 'ur"""': double3,
                           "R'''": single3, 'R"""': double3,
                           "U'''": single3, 'U"""': double3,
                           "uR'''": single3, 'uR"""': double3,
                           "Ur'''": single3, 'Ur"""': double3,
                           "UR'''": single3, 'UR"""': double3,
                           }

closurePairs= {')':'(',']':'[','}':'{'}
closurePairsRev= {'(':')','[':']','{':'}'}

##################################################
## Regex chunks for the parser ##

tripleQuotedStringREs = {}
def makeTripleQuoteRe(start, end):
    start = escapeRegexChars(start)
    end = escapeRegexChars(end)
    return re.compile(r'(?:' + start + r').*?' + r'(?:' + end + r')', re.DOTALL)

for start, end in tripleQuotedStringPairs.items():
    tripleQuotedStringREs[start] = makeTripleQuoteRe(start, end)

WS = r'[ \f\t]*'  
EOL = r'\r\n|\n|\r'
EOLZ = EOL + r'|\Z'
escCharLookBehind = nongroup(r'(?<=\A)',r'(?<!\\)')
nameCharLookAhead = r'(?=[A-Za-z_])'
identRE=re.compile(r'[a-zA-Z_][a-zA-Z_0-9]*')
EOLre=re.compile(r'(?:\r\n|\r|\n)')

specialVarRE=re.compile(r'([a-zA-z_]+)@') # for matching specialVar comments
# e.g. ##author@ Tavis Rudd

##################################################
## CLASSES ##

class ParseError(ValueError):
    def __init__(self, stream, msg='Invalid Syntax', extMsg=''):
        self.stream = stream
        if stream.pos() >= len(stream):
            stream.setPos(len(stream) -1)
        self.msg = msg
        self.extMsg = extMsg
        
    def __str__(self):
        return self.report()

    def report(self):
        stream = self.stream
        if stream.filename():
            f = " in file %s" % stream.filename()
        else:
            f = ''
        report = ''
        row, col, line = self.stream.getRowColLine()


        ## get the surrounding lines
        lines = stream.splitlines()
        prevLines = []                  # (rowNum, content)
        for i in range(1,4):
            if row-1-i <=0:
                break
            prevLines.append( (row-i,lines[row-1-i]) )

        nextLines = []                  # (rowNum, content)
        for i in range(1,4):
            if not row-1+i < len(lines):
                break
            nextLines.append( (row+i,lines[row-1+i]) )
        nextLines.reverse()
        
        ## print the main message
        report += "\n\n%s. Line %i, column %i%s\n\n" % (self.msg, row, col, f)
        report += 'Line|Line contents\n'
        report += '----|-------------------------------------------------------------\n'
        while prevLines:
            lineInfo = prevLines.pop()
            report += "%(row)-4d|%(line)s\n"% {'row':lineInfo[0], 'line':lineInfo[1]}
        report += "%(row)-4d|%(line)s\n"% {'row':row, 'line':line}
        report += ' '*5 +' '*(col-1) + "^\n"
        
        while nextLines:
            lineInfo = nextLines.pop()
            report += "%(row)-4d|%(line)s\n"% {'row':lineInfo[0], 'line':lineInfo[1]}
        ## add the extra msg
        if self.extMsg:
            report += self.extMsg + '\n'
            
        return report

class ForbiddenSyntax(ParseError): pass
class ForbiddenExpression(ForbiddenSyntax): pass
class ForbiddenDirective(ForbiddenSyntax): pass

class CheetahVariable:
    def __init__(self, nameChunks, useNameMapper=True, cacheToken=None,
                 rawSource=None):
        self.nameChunks = nameChunks
        self.useNameMapper = useNameMapper
        self.cacheToken = cacheToken
        self.rawSource = rawSource
        
class Placeholder(CheetahVariable): pass

class ArgList:
    """Used by _LowLevelParser.getArgList()"""

    def __init__(self):
        self.argNames = []
        self.defVals = []
        self.i = 0

    def addArgName(self, name):
        self.argNames.append( name )
        self.defVals.append( None )

    def next(self):
        self.i += 1

    def addToDefVal(self, token):
        i = self.i
        if self.defVals[i] == None:
            self.defVals[i] = ''
        self.defVals[i] += token
    
    def merge(self):
        defVals = self.defVals
        for i in range(len(defVals)):
            if type(defVals[i]) == StringType:
                defVals[i] = defVals[i].strip()
                
        return map(None, [i.strip() for i in self.argNames], defVals)
    
    def __str__(self):
        return str(self.merge())
    
class _LowLevelParser(SourceReader):
    """This class implements the methods to match or extract ('get*') the basic
    elements of Cheetah's grammar.  It does NOT handle any code generation or
    state management.
    """

    _settingsManager = None

    def setSettingsManager(self, settingsManager):
        self._settingsManager = settingsManager
        
    def setting(self, key): 
        return self._settingsManager.setting(key)
        
    def setSetting(self, key, val):
        self._settingsManager.setSetting(key, val)
        
    def updateSettings(self, settings):
        self._settingsManager.updateSettings(settings)

    def _initializeSettings(self): 
        self._settingsManager._initializeSettings()
    
    def configureParser(self):
        """Is called by the Compiler instance after the parser has had a
        settingsManager assigned with self.setSettingsManager() 
        """
        self._makeCheetahVarREs()
        self._makeCommentREs()
        self._makeDirectiveREs()
        self._makePspREs()
        self._possibleNonStrConstantChars = (
            self.setting('commentStartToken')[0] +
            self.setting('multiLineCommentStartToken')[0] + 
            self.setting('cheetahVarStartToken')[0] +
            self.setting('directiveStartToken')[0] +
            self.setting('PSPStartToken')[0])
        self._nonStrConstMatchers = [
            self.matchCommentStartToken,
            self.matchMultiLineCommentStartToken,
            self.matchVariablePlaceholderStart,
            self.matchExpressionPlaceholderStart,
            self.matchDirective,
            self.matchPSPStartToken,
            self.matchEOLSlurpToken,
            ]

    ## regex setup ##

    def _makeCheetahVarREs(self):
        
        """Setup the regexs for Cheetah $var parsing."""

        num = r'[0-9\.]+'
        interval =   (r'(?P<interval>' + 
                      num + r's|' +
                      num + r'm|' +
                      num + r'h|' +
                      num + r'd|' +
                      num + r'w|' +
                      num + ')' 
                      )
    
        cacheToken = (r'(?:' +
                      r'(?P<REFRESH_CACHE>\*' + interval + '\*)'+
                      '|' +
                      r'(?P<STATIC_CACHE>\*)' +
                      '|' +                      
                      r'(?P<NO_CACHE>)' +
                      ')')
        self.cacheTokenRE = re.compile(cacheToken)
        
        self.cheetahVarStartRE = re.compile(
            escCharLookBehind +
            r'(?P<startToken>' +
            escapeRegexChars(self.setting('cheetahVarStartToken')) +
            ')' +
            r'(?P<cacheToken>' +
            cacheToken +
            ')' +
            r'(?P<enclosure>|(?:(?:\{|\(|\[)[ \t\f]*))' + # allow WS after enclosure
            r'(?=[A-Za-z_])')
        validCharsLookAhead = r'(?=[A-Za-z_\*!\{\(\[])'
        self.cheetahVarStartToken = self.setting('cheetahVarStartToken')
        self.cheetahVarStartTokenRE = re.compile(
            escCharLookBehind +
            escapeRegexChars(self.setting('cheetahVarStartToken'))
            +validCharsLookAhead
            )

        self.expressionPlaceholderStartRE = re.compile(
            escCharLookBehind +
            r'(?P<startToken>' + escapeRegexChars(self.setting('cheetahVarStartToken')) + ')' +
            r'(?P<cacheToken>' + cacheToken + ')' +
            #r'\[[ \t\f]*'
            r'(?:\{|\(|\[)[ \t\f]*'
            + r'(?=[^\)\}\]])'
            )

        if self.setting('EOLSlurpToken'):
            self.EOLSlurpRE = re.compile(
                escapeRegexChars(self.setting('EOLSlurpToken'))
                + r'[ \t\f]*'
                + r'(?:'+EOL+')'
                )
        else:
            self.EOLSlurpRE = None


    def _makeCommentREs(self):
        """Construct the regex bits that are used in comment parsing."""
        
        startTokenEsc = escapeRegexChars(self.setting('commentStartToken'))
        self.commentStartTokenRE = re.compile(escCharLookBehind + startTokenEsc)
        del startTokenEsc
        
        startTokenEsc = escapeRegexChars(
            self.setting('multiLineCommentStartToken'))
        endTokenEsc = escapeRegexChars(
            self.setting('multiLineCommentEndToken'))
        self.multiLineCommentTokenStartRE = re.compile(escCharLookBehind +
                                                       startTokenEsc)
        self.multiLineCommentEndTokenRE = re.compile(escCharLookBehind +
                                                     endTokenEsc)
        
    def _makeDirectiveREs(self):
        
        """Construct the regexs that are used in directive parsing."""
        
        startToken = self.setting('directiveStartToken')
        endToken = self.setting('directiveEndToken')
        startTokenEsc = escapeRegexChars(startToken)
        endTokenEsc = escapeRegexChars(endToken)
        
        validSecondCharsLookAhead = r'(?=[A-Za-z_])'
        self.directiveStartTokenRE = re.compile(escCharLookBehind + startTokenEsc
                                                + validSecondCharsLookAhead)
        self.directiveEndTokenRE = re.compile(escCharLookBehind + endTokenEsc)

    def _makePspREs(self):
        
        """Setup the regexs for PSP parsing."""
        
        startToken = self.setting('PSPStartToken')
        startTokenEsc = escapeRegexChars(startToken)
        self.PSPStartTokenRE = re.compile(escCharLookBehind + startTokenEsc)
        
        endToken = self.setting('PSPEndToken')
        endTokenEsc = escapeRegexChars(endToken)
        self.PSPEndTokenRE = re.compile(escCharLookBehind + endTokenEsc)


    def matchPyToken(self):
        match = pseudoprog.match(self.src(), self.pos())
        
        if match and match.group() in tripleQuotedStringStarts:
            TQSmatch = tripleQuotedStringREs[match.group()].match(self.src(), self.pos())
            if TQSmatch:
                return TQSmatch
        return match
        
    def getPyToken(self):
        match = self.matchPyToken()
        if match is None:
            from Parser import ParseError
            raise ParseError(self)
        elif match.group() in tripleQuotedStringStarts:
            from Parser import ParseError
            raise ParseError(self, msg='Malformed triple-quoted string')
        return self.readTo(match.end())

    def matchNonStrConst(self):
        if self.peek() in self._possibleNonStrConstantChars:
            for matcher in self._nonStrConstMatchers:
                if matcher():
                    return True
        return False

    def matchEOLSlurpToken(self):
        if self.EOLSlurpRE:
            return self.EOLSlurpRE.match(self.src(), self.pos())

    def getEOLSlurpToken(self):
        match = self.matchEOLSlurpToken()
        if not match:
            raise ParseError(self, msg='Invalid EOL slurp token')
        return self.readTo(match.end())

    def matchCommentStartToken(self):
        return self.commentStartTokenRE.match(self.src(), self.pos())
    
    def getCommentStartToken(self):
        match = self.matchCommentStartToken()
        if not match:
            raise ParseError(self, msg='Invalid single-line comment start token')
        return self.readTo(match.end())

    def matchMultiLineCommentStartToken(self):
        return self.multiLineCommentTokenStartRE.match(self.src(), self.pos())
    
    def getMultiLineCommentStartToken(self):
        match = self.matchMultiLineCommentStartToken()
        if not match:
            raise ParseError(self, msg='Invalid multi-line comment start token')
        return self.readTo(match.end())

    def matchMultiLineCommentEndToken(self):
        return self.multiLineCommentEndTokenRE.match(self.src(), self.pos())
    
    def getMultiLineCommentEndToken(self):
        match = self.matchMultiLineCommentEndToken()
        if not match:
            raise ParseError(self, msg='Invalid multi-line comment end token')
        return self.readTo(match.end())

    def isLineClearToStartToken(self, pos=None):
        if pos == None:
            pos = self.pos()
        self.checkPos(pos)            
        src = self.src()
        BOL = self.findBOL()
        return BOL == pos or src[BOL:pos].isspace()

    def matchWhiteSpace(self, WSchars=' \f\t'):
        return (not self.atEnd()) and  self.peek() in WSchars

    def getWhiteSpace(self, WSchars=' \f\t'):
        if not self.matchWhiteSpace(WSchars):
            return ''
        start = self.pos()
        while self.pos() < self.breakPoint():
            self.advance()
            if not self.matchWhiteSpace(WSchars):
                break
        return self.src()[start:self.pos()]

    def matchNonWhiteSpace(self, WSchars=' \f\t\n\r'):
        return self.atEnd() or not self.peek() in WSchars

    def getNonWhiteSpace(self, WSchars=' \f\t\n\r'):
        if not self.matchNonWhiteSpace(WSchars):
            return ''
        start = self.pos()
        while self.pos() < self.breakPoint():
            self.advance()
            if not self.matchNonWhiteSpace(WSchars):
                break
        return self.src()[start:self.pos()]
    
    def getDottedName(self):
        srcLen = len(self)
        nameChunks = []
        
        if not self.peek() in identchars:
            raise ParseError(self)
    
        while self.pos() < srcLen:
            c = self.peek()
            if c in namechars:
                nameChunk = self.getIdentifier()
                nameChunks.append(nameChunk)
            elif c == '.':
                if self.pos()+1 <srcLen and self.peek(1) in identchars:
                    nameChunks.append(self.getc())
                else:
                    break
            else:
                break

        return ''.join(nameChunks)

    def matchIdentifier(self):
        return identRE.match(self.src(), self.pos())
    
    def getIdentifier(self):
        match = self.matchIdentifier()
        if not match:
            raise ParseError(self, msg='Invalid identifier')
        return self.readTo(match.end())

    def matchOperator(self):
        match = self.matchPyToken()
        if match and match.group() not in operators:
            match = None
        return match

    def getOperator(self):
        match = self.matchOperator()
        if not match:
            raise ParseError(self, msg='Expected operator')
        return self.readTo( match.end() )

    def matchAssignmentOperator(self):
        match = self.matchPyToken()
        if match and match.group() not in assignmentOps:
            match = None
        return match
        
    def getAssignmentOperator(self):
        match = self.matchAssignmentOperator()
        if not match:
            raise ParseError(self, msg='Expected assignment operator')
        return self.readTo( match.end() )

    def matchDirective(self, directiveKeyChars=identchars+'-'):
        """Returns False or the name of the directive matched.
        """
        startPos = self.pos()
        if not self.matchDirectiveStartToken():
            return False

        self.getDirectiveStartToken()
        directiveKey = ''
        while self.pos() < self.breakPoint():
            c = self.getc()
            if not c in directiveKeyChars:
                break
            directiveKey += c
            
        if not directiveKey in self._directiveEaters.keys():
            directiveKey= False
        self.setPos(startPos)
        # DEBUG: print directiveKey, self.src()
        return directiveKey        
            
    def matchDirectiveStartToken(self):
        return self.directiveStartTokenRE.match(self.src(), self.pos())
    
    def getDirectiveStartToken(self):
        match = self.matchDirectiveStartToken()
        if not match:
            raise ParseError(self, msg='Invalid directive start token')
        return self.readTo(match.end())

    def matchDirectiveEndToken(self):
        return self.directiveEndTokenRE.match(self.src(), self.pos())
    
    def getDirectiveEndToken(self):
        match = self.matchDirectiveEndToken()
        if not match:
            raise ParseError(self, msg='Invalid directive end token')
        return self.readTo(match.end())

    def matchPSPStartToken(self):
        return self.PSPStartTokenRE.match(self.src(), self.pos())

    def matchPSPEndToken(self):
        return self.PSPEndTokenRE.match(self.src(), self.pos())

    def getPSPStartToken(self):
        match = self.matchPSPStartToken()
        if not match:
            raise ParseError(self, msg='Invalid psp start token')
        return self.readTo(match.end())

    def getPSPEndToken(self):
        match = self.matchPSPEndToken()
        if not match:
            raise ParseError(self, msg='Invalid psp end token')
        return self.readTo(match.end())

    def matchCheetahVarStart(self):
        """includes the enclosure and cache token"""
        return self.cheetahVarStartRE.match(self.src(), self.pos())

    def matchCheetahVarStartToken(self):
        """includes the enclosure and cache token"""
        return self.cheetahVarStartTokenRE.match(self.src(), self.pos())

    def matchVariablePlaceholderStart(self):
        """includes the enclosure and cache token"""
        return self.cheetahVarStartRE.match(self.src(), self.pos())

    def matchExpressionPlaceholderStart(self):
        """includes the enclosure and cache token"""
        return self.expressionPlaceholderStartRE.match(self.src(), self.pos())        

    def getCheetahVarStartToken(self):
        """just the start token, not the enclosure or cache token"""
        match = self.matchCheetahVarStartToken()
        if not match:
            raise ParseError(self, msg='Expected Cheetah $var start token')            
        return self.readTo( match.end() )
        
    def _X_getCheetahVarStartToken(self):
        """just the start token, not the enclosure or cache token"""
        match = self.matchCheetahVarStart()
        if not match:
            raise ParseError(self, msg='Expected Cheetah $var start token')            
        return self.readTo( match.end('startToken') )


    def getCacheToken(self):
        try:
            token = self.cacheTokenRE.match(self.src(), self.pos())
            self.setPos( token.end() )
            return token.group()
        except:
            raise ParseError(self, msg='Expected cache token')


    def getTargetVarsList(self):
        varnames = []
        while not self.atEnd():
            if self.peek() in ' \t\f':
                self.getWhiteSpace()
            elif self.peek() in '\r\n':
                break
            elif self.startswith(','):
                self.advance()
            elif self.startswith('in ') or self.startswith('in\t'):
                break
            elif self.matchCheetahVarStart():
                self.getCheetahVarStartToken()
                self.getCacheToken()
                varnames.append( self.getDottedName() )
            elif self.matchIdentifier():
                varnames.append( self.getDottedName() )
            else:
                break
        return varnames
        
    def getCheetahVar(self, plain=False, skipStartToken=False):
        """This is called when parsing inside expressions. Cache tokens are only
        valid in placeholders so this method discards any cache tokens found.
        """
        if not skipStartToken:
            self.getCheetahVarStartToken()
        self.getCacheToken()
        return self.getCheetahVarBody(plain=plain)

    def _X_getCheetahVar(self, plain=False, skipStartToken=False):
        # @@TR: refactoring in progress
        if not skipStartToken:
            self.getCheetahVarStartToken()
        cacheToken = self.getCacheToken()
        return CheetahVariable(nameChunks=self.getCheetahVarNameChunks(),
                               cacheToken=cacheToken,
                               useNameMapper=plain
                               )
            
    def getCheetahVarBody(self, plain=False):
        # @@TR: this should be in the compiler
        return self._compiler.genCheetahVar(self.getCheetahVarNameChunks(), plain=plain)
        
    def getCheetahVarNameChunks(self):
        
        """
        nameChunks = list of Cheetah $var subcomponents represented as tuples
          [ (namemapperPart,autoCall,restOfName),
          ]
        where:
          namemapperPart = the dottedName base
          autocall = where NameMapper should use autocalling on namemapperPart
          restOfName = any arglist, index, or slice

        If restOfName contains a call arglist (e.g. '(1234)') then autocall is
        False, otherwise it defaults to True.

        EXAMPLE
        ------------------------------------------------------------------------

        if the raw CheetahVar is
          $a.b.c[1].d().x.y.z
          
        nameChunks is the list
          [ ('a.b.c',True,'[1]'),
            ('d',False,'()'),     
            ('x.y.z',True,''),   
          ]

        """

        chunks = []
        while self.pos() < len(self):
            rest = ''
            autoCall = True
            if not self.peek() in identchars + '.':
                break
            elif self.peek() == '.':
                
                if self.pos()+1 < len(self) and self.peek(1) in identchars:
                    self.advance()  # discard the period as it isn't needed with NameMapper
                else:
                    break
                
            dottedName = self.getDottedName()
            if not self.atEnd() and self.peek() in '([':
                if self.peek() == '(':
                    rest = self.getCallArgString()
                else:
                    rest = self.getExpression(enclosed=True)
                
                period = max(dottedName.rfind('.'), 0)
                if period:
                    chunks.append( (dottedName[:period], autoCall, '') )
                    dottedName = dottedName[period+1:]
                if rest and rest[0]=='(':
                    autoCall = False
            chunks.append( (dottedName, autoCall, rest) )

        return chunks
    

    def getCallArgString(self,
                         enclosures=[],  # list of tuples (char, pos), where char is ({ or [ 
                         ):

        """ Get a method/function call argument string. 

        This method understands *arg, and **kw
        """
        
        if enclosures:
            pass
        else:
            if not self.peek() == '(':
                raise ParseError(self, msg="Expected '('")
            startPos = self.pos()
            self.getc()
            enclosures = [('(', startPos),
                          ]
        
        argStringBits = ['(']
        addBit = argStringBits.append

        while 1:
            if self.atEnd():
                open = enclosures[-1][0]
                close = closurePairsRev[open]
                self.setPos(enclosures[-1][1])
                raise ParseError(
                    self, msg="EOF was reached before a matching '" + close +
                    "' was found for the '" + open + "'")

            c = self.peek()
            if c in ")}]": # get the ending enclosure and break                
                if not enclosures:
                    raise ParseError(self)
                c = self.getc()
                open = closurePairs[c]
                if enclosures[-1][0] == open:
                    enclosures.pop()
                    addBit(')')  
                    break
                else:
                    raise ParseError(self)
            elif c in " \t\f\r\n":
                addBit(self.getc())
            elif self.matchCheetahVarStart():
                startPos = self.pos()
                codeFor1stToken = self.getCheetahVar()
                WS = self.getWhiteSpace()
                if not self.atEnd() and self.peek() == '=':
                    nextToken = self.getPyToken()
                    if nextToken == '=':
                        endPos = self.pos()
                        self.setPos(startPos)
                        codeFor1stToken = self.getCheetahVar(plain=True)
                        self.setPos(endPos)
                        
                    ## finally
                    addBit( codeFor1stToken + WS + nextToken )
                else:
                    addBit( codeFor1stToken + WS)
            else:
                beforeTokenPos = self.pos()
                token = self.getPyToken()
                if token in ('{','(','['):
                    self.rev()
                    token = self.getExpression(enclosed=True)
                token = self.transformToken(token, beforeTokenPos)
                addBit(token)

        return ''.join(argStringBits)
    
    def getDefArgList(self, exitPos=None, useNameMapper=False):

        """ Get an argument list. Can be used for method/function definition
        argument lists or for #directive argument lists. Returns a list of
        tuples in the form (argName, defVal=None) with one tuple for each arg
        name.

        These defVals are always strings, so (argName, defVal=None) is safe even
        with a case like (arg1, arg2=None, arg3=1234*2), which would be returned as
        [('arg1', None),
         ('arg2', 'None'),
         ('arg3', '1234*2'),         
        ]

        This method understands *arg, and **kw

        """

        if self.peek() == '(':
            self.advance()
        else:
            exitPos = self.findEOL()  # it's a directive so break at the EOL
        argList = ArgList()
        onDefVal = False

        # @@TR: this settings mangling should be removed
        useNameMapper_orig = self.setting('useNameMapper')
        self.setSetting('useNameMapper', useNameMapper)

        while 1:
            if self.atEnd():
                self.setPos(enclosures[-1][1])
                raise ParseError(
                    self, msg="EOF was reached before a matching ')'"+
                    " was found for the '('")

            if self.pos() == exitPos:
                break

            c = self.peek()
            if c == ")" or self.matchDirectiveEndToken():
                break
            elif c in " \t\f\r\n":
                if onDefVal:
                    argList.addToDefVal(c)
                self.advance()
            elif c == '=':
                onDefVal = True
                self.advance()
            elif c == ",":
                argList.next()
                onDefVal = False
                self.advance()
            elif self.startswith(self.cheetahVarStartToken) and not onDefVal:
                self.advance(len(self.cheetahVarStartToken))
            elif self.matchIdentifier() and not onDefVal:
                argList.addArgName( self.getIdentifier() )
            elif onDefVal:
                if self.matchCheetahVarStart():
                    token = self.getCheetahVar()
                else:
                    beforeTokenPos = self.pos()
                    token = self.getPyToken()
                    if token in ('{','(','['):
                        self.rev()
                        token = self.getExpression(enclosed=True)
                    token = self.transformToken(token, beforeTokenPos)
                argList.addToDefVal(token)
            elif c == '*' and not onDefVal:
                varName = self.getc()
                if self.peek() == '*':
                    varName += self.getc()
                if not self.matchIdentifier():
                    raise ParseError(self)
                varName += self.getIdentifier()
                argList.addArgName(varName)
            else:
                raise ParseError(self)

                
        self.setSetting('useNameMapper', useNameMapper_orig) # @@TR: see comment above
        return argList.merge()
    
    def getExpressionParts(self,
                           enclosed=False, 
                           enclosures=None, # list of tuples (char, pos), where char is ({ or [ 
                           pyTokensToBreakAt=None
                           ):

        """ Get a Cheetah expression that includes $CheetahVars and break at
        directive end tokens, the end of an enclosure, or at a specified
        pyToken.
        """

        if enclosures is None:
            enclosures = []
        
        srcLen = len(self)
        exprBits = []
        while 1:
            if self.atEnd():
                if enclosures:
                    open = enclosures[-1][0]
                    close = closurePairsRev[open]
                    self.setPos(enclosures[-1][1])
                    raise ParseError(
                        self, msg="EOF was reached before a matching '" + close +
                        "' was found for the '" + open + "'")
                else:
                    break

            c = self.peek()
            if c in "{([":
                exprBits.append(c)
                enclosures.append( (c, self.pos()) )
                self.advance()                
            elif enclosed and not enclosures:
                break                
            elif c in "])}":
                if not enclosures:
                    raise ParseError(self)
                open = closurePairs[c]
                if enclosures[-1][0] == open:
                    enclosures.pop()
                    exprBits.append(c)
                else:
                    open = enclosures[-1][0]
                    close = closurePairsRev[open]
                    row, col = self.getRowCol()
                    self.setPos(enclosures[-1][1])
                    raise ParseError(
                        self, msg= "A '" + c + "' was found at line " + str(row) +
                        ", col " + str(col) +
                        " before a matching '" + close +
                        "' was found\nfor the '" + open + "'")
                self.advance()
                                
            elif c in " \f\t":
                exprBits.append(self.getWhiteSpace())            
            elif self.matchDirectiveEndToken() and not enclosures:
                break            
            elif c == "\\" and self.pos()+1 < srcLen:
                eolMatch = EOLre.match(self.src(), self.pos()+1)
                if not eolMatch:
                    self.advance()
                    raise ParseError(self, msg='Line ending expected')
                self.setPos( eolMatch.end() )
            elif c in '\r\n':
                if enclosures:
                    self.advance()                    
                else:
                    break                    
            elif self.matchCheetahVarStart():
                token = self.getCheetahVar()
                exprBits.append(token)
            else:                
                beforeTokenPos = self.pos()
                token = self.getPyToken()
                if pyTokensToBreakAt and token in pyTokensToBreakAt:
                    self.setPos(beforeTokenPos)
                    break

                token = self.transformToken(token, beforeTokenPos)
                        
                exprBits.append(token)                    
                if identRE.match(token):
                    if token == 'for':
                        # @@TR: this needs expanding to handle more complex targetVarLists
                        targetVars = self.getTargetVarsList()
                        exprBits.append(' ' + ', '.join(targetVars) + ' ')
                    else:
                        exprBits.append(self.getWhiteSpace())
                        if not self.atEnd() and self.peek() == '(':
                            exprBits.append(self.getCallArgString())                    
        ##
        return exprBits

    def getExpression(self,
                      enclosed=False, 
                      enclosures=None, # list of tuples (char, pos), where # char is ({ or [
                      pyTokensToBreakAt=None
                      ):
        """Returns the output of self.getExpressionParts() as a concatenated
        string rather than as a list.
        """
        return ''.join(self.getExpressionParts(enclosed=enclosed, enclosures=enclosures,
                                               pyTokensToBreakAt=pyTokensToBreakAt))


    def transformToken(self, token, beforeTokenPos):
        if token=='c' and not self.atEnd() and self.peek() in '\'"':
            nextToken = self.getPyToken()
            token = nextToken.upper()
            theStr = eval(token)
            endPos = self.pos()
            if not theStr:
                return
            
            if token.startswith(single3) or token.startswith(double3):
                startPosIdx = 3
            else:
                startPosIdx = 1
            #print 'CHEETAH STRING', nextToken, theStr, startPosIdx
            self.setPos(beforeTokenPos+startPosIdx+1)
            outputExprs = []
            strConst = ''
            while self.pos() < (endPos-startPosIdx):
                if self.matchCheetahVarStart():
                    if strConst:
                        outputExprs.append(repr(strConst))
                        strConst = ''
                    placeholderExpr = self.getPlaceholder()#self.getCheetahVar()
                    outputExprs.append('str('+placeholderExpr+')')
                else:
                    strConst += self.getc()
            self.setPos(endPos)
            if strConst:
                outputExprs.append(repr(strConst))
            #if not self.atEnd() and self.matches('.join('):
            #    print 'DEBUG***'
            token = "''.join(["+','.join(outputExprs)+"])"
        return token

    def getPlaceholder(self, allowCacheTokens=False, plain=False, returnEverything=False):
        # filtered 
        for callback in self.setting('preparsePlaceholderHooks'):
            callback(parser=self)

        startPos = self.pos()
        lineCol = self.getRowCol(startPos)
        startToken = self.getCheetahVarStartToken()
        if allowCacheTokens:
            cacheToken = self.getCacheToken()
            cacheTokenParts = self.cacheTokenRE.match(cacheToken).groupdict()        
        else:
            cacheTokenParts = {}

        if self.peek() in '({[':         
            pos = self.pos()
            enclosureOpenChar = self.getc()
            enclosures = [ (enclosureOpenChar, pos) ]
            self.getWhiteSpace()
        else:
            enclosures = []

        filterArgs = None
        if self.matchIdentifier(): 
            nameChunks = self.getCheetahVarNameChunks()
            expr = self._compiler.genCheetahVar(nameChunks[:], plain=plain)
            restOfExpr = None
            if enclosures:
                WS = self.getWhiteSpace()
                expr += WS
                if self.setting('allowPlaceholderFilterArgs') and self.peek()==',':
                    filterArgs = self.getCallArgString(enclosures=enclosures)[1:-1]
                else:
                    if self.peek()==closurePairsRev[enclosureOpenChar]:
                        self.getc()
                    else:
                        restOfExpr = self.getExpression(enclosed=True, enclosures=enclosures)
                        if restOfExpr[-1] == closurePairsRev[enclosureOpenChar]:
                            restOfExpr = restOfExpr[:-1]
                        expr += restOfExpr
            rawPlaceholder = self[startPos: self.pos()]
            exprToFilter = self._compiler.genPlainVar(nameChunks[:])
            if restOfExpr:
                exprToFilter = exprToFilter + WS + restOfExpr
        else:
            expr = self.getExpression(enclosed=True, enclosures=enclosures)
            if expr[-1] == closurePairsRev[enclosureOpenChar]:
                expr = expr[:-1]
            rawPlaceholder=self[startPos: self.pos()]
            exprToFilter = expr
            
        self._applyExpressionFilters(expr,'placeholder',rawExpr=rawPlaceholder,startPos=startPos)
        for callback in self.setting('postparsePlaceholderHooks'):
            callback(parser=self)

        if returnEverything:
            return expr, rawPlaceholder, lineCol, cacheTokenParts, filterArgs
        else:
            return expr
        

class _HighLevelParser(_LowLevelParser):
    """This class is a StateMachine for parsing Cheetah source and
    sending state dependent code generation commands to
    Cheetah.Compiler.Compiler.
    """
    def __init__(self, src, filename=None, breakPoint=None, compiler=None):
        SourceReader.__init__(self, src, filename=filename, breakPoint=breakPoint)
        self.setSettingsManager(compiler)
        self._compiler = compiler
        self.configureParser()
        self.initDirectives()
        self.setupState()

        #self._reader = SourceReader(src, filename=filename, breakPoint=breakPoint)
        #for k, v in inspect.getmembers(self._reader, inspect.ismethod):
        #    setattr(self, k, v)

    def setupState(self):
        self._indentStack = []
            
    def initDirectives(self):
        self._directiveEaters = {

            # importing and inheritance
            'import':self.eatImport,
            'from':self.eatImport,
            'extends': self.eatExtends,
            'implements': self.eatImplements,

            # output, filtering, and caching
            'slurp': self.eatSlurp,
            'raw': self.eatRaw,
            'include': self.eatInclude,
            'cache': self.eatCache,
            'call': self.eatCall,
            'arg': self.eatCallArg,
            'filter': self.eatFilter,
            'echo': self.eatEcho,
            'silent': self.eatSilent,

            # declaration, assignment, and deletion
            'attr':self.eatAttr,
            'def': self.eatDef,
            'block': self.eatBlock,

            'closure': self.eatClosure,

            'set': self.eatSet,
            'del': self.eatDel,
            
            # flow control
            'while': self.eatWhile,
            'for': self.eatFor,
            'if': self.eatIf,
            'else': self.eatElse,
            'elif': self.eatElse,
            'pass': self.eatPass,
            'break': self.eatBreak,
            'continue': self.eatContinue,
            'stop': self.eatStop,
            'return': self.eatReturn,

            # little wrappers
            'repeat': self.eatRepeat,
            'unless': self.eatUnless,

            # error handling
            'assert': self.eatAssert,
            'raise': self.eatRaise,
            'try': self.eatTry,
            'except': self.eatExcept,
            'finally': self.eatFinally,
            'errorCatcher':self.eatErrorCatcher,

            # intructions to the parser and compiler
            'breakpoint':self.eatBreakPoint,
            'compiler':self.eatCompiler,            
            'compiler-settings':self.eatCompilerSettings,
            
            # misc
            'shBang': self.eatShbang,
            'encoding': self.eatEncoding,
            
            'end': self.eatEndDirective,
            }

        
        self._directiveEndEaters = {
            'def': self.eatEndDef,
            'block': self.eatEndBlock,
            'closure': self.eatEndClosure,
            
            'cache': self.eatEndCache,
            'call': self.eatEndCall,
            'filter': self.eatEndFilter,
            'errorCatcher':self.eatEndErrorCatcher,
            
            'while': self.eatEndWhile,
            'for': self.eatEndFor,
            'if': self.eatEndIf,
            'try': self.eatEndTry,
            
            'repeat': self.eatEndRepeat,
            'unless': self.eatEndUnless,
            
            }
        self._indentingDirectives = ['def','block','closure',                                     
                                     'if','for','while',
                                     'try',
                                     'repeat','unless']

    def _applyExpressionFilters(self, expr, exprType, rawExpr=None, startPos=None):
        for callback in self.setting('expressionFilterHooks'):
            callback(parser=self, expr=expr,  exprType=exprType,
                     rawExpr=rawExpr, startPos=startPos)

    def _filterDisabledDirectives(self, directiveKey):
        directiveKey = directiveKey.lower()
        if (directiveKey in self.setting('disabledDirectives')
            or (self.setting('enabledDirectives')
                and directiveKey not in self.setting('enabledDirectives'))):
            for callback in self.setting('disabledDirectiveHooks'):
                callback(parser=self, directiveKey=directiveKey)
            raise ForbiddenDirective(self, msg='This %r directive is disabled'%directiveKey)
        
    ## main parse loop

    def parse(self, endPos=None, assertEmptyStack=True):
        while not self.atEnd() and not (endPos and self.pos()==endPos):
            #print self.peek(), self.pos() #DEBUG
            if self.matchCommentStartToken():
                self.eatComment()
            elif self.matchMultiLineCommentStartToken():
                self.eatMultiLineComment()
            elif self.matchVariablePlaceholderStart():
                self.eatPlaceholder()
            elif self.matchExpressionPlaceholderStart():
                self.eatPlaceholder()
            elif self.matchDirective():
                self.eatDirective()
            elif self.matchPSPStartToken():
                self.eatPSP()
            elif self.matchEOLSlurpToken():
                self.eatEOLSlurpToken()
            else:
                self.eatStrConstant()
        if assertEmptyStack:
            self.assertEmptyIndentStack()
                
    ## eat methods    
                
    def eatStrConstant(self):
        startPos = self.pos()
        while not self.atEnd():
            if self.matchNonStrConst():
                break
            else:
                self.advance()
        strConst = self.readTo(self.pos(), start=startPos)
        self._compiler.addStrConst(strConst)

    def eatComment(self):
        isLineClearToStartToken = self.isLineClearToStartToken()
        if isLineClearToStartToken:
            self._compiler.handleWSBeforeDirective()
        self.getCommentStartToken()            
        comm = self.readToEOL(gobble=isLineClearToStartToken)
        self._compiler.addComment(comm)

    def eatMultiLineComment(self):
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLine = self.findEOL()

        self.getMultiLineCommentStartToken()
        endPos = startPos = self.pos()
        level = 1
        while 1:
            endPos = self.pos()
            if self.atEnd():
                break
            if self.matchMultiLineCommentStartToken():
                self.getMultiLineCommentStartToken()
                level += 1
            elif self.matchMultiLineCommentEndToken():
                self.getMultiLineCommentEndToken()
                level -= 1
            if not level:
                break
            self.advance()
        comm = self.readTo(endPos, start=startPos)
        if not self.atEnd():
            self.getMultiLineCommentEndToken()
        # don't gobble
        self._compiler.addComment(comm)

    def eatPlaceholder(self):
        (expr, rawPlaceholder,
         lineCol, cacheTokenParts,
         filterArgs) = self.getPlaceholder(allowCacheTokens=True, returnEverything=True)
        self._compiler.addPlaceholder(
            expr,
            filterArgs=filterArgs,
            rawPlaceholder=rawPlaceholder,
            cacheTokenParts=cacheTokenParts,
            lineCol=lineCol)
        return
        
    def eatPSP(self):
        # filtered
        self._filterDisabledDirectives(directiveKey='psp')
        self.getPSPStartToken()
        endToken = self.setting('PSPEndToken')
        startPos = self.pos()            
        while not self.atEnd():
            if self.peek() == endToken[0]:
                if self.matchPSPEndToken():
                    break
            self.advance()
        pspString = self.readTo(self.pos(), start=startPos).strip()
        self._applyExpressionFilters(pspString, 'psp', startPos=startPos)
        self._compiler.addPSP(pspString)
        self.getPSPEndToken()


    ## generic directive eat methods

    def eatDirective(self):
        directiveKey = self.matchDirective()
        self._filterDisabledDirectives(directiveKey)

        for callback in self.setting('preparseDirectiveHooks'):
            callback(parser=self, directiveKey=directiveKey)

        self._directiveEaters[directiveKey]()

        for callback in self.setting('postparseDirectiveHooks'):
            callback(parser=self, directiveKey=directiveKey)

    def _eatRestOfDirectiveTag(self, isLineClearToStartToken, endOfFirstLinePos):
        if self.matchDirectiveEndToken():
            self.getDirectiveEndToken()
        elif isLineClearToStartToken and (not self.atEnd()) and self.peek() in '\r\n':
            self.readToEOL(gobble=True)
            
        if isLineClearToStartToken and (self.atEnd() or self.pos() > endOfFirstLinePos):
            self._compiler.handleWSBeforeDirective() #command to the compiler

    def eatEndDirective(self):
        isLineClearToStartToken = self.isLineClearToStartToken()
        self.getDirectiveStartToken()
        self.advance(3)                 # to end of 'end'
        self.getWhiteSpace()
        pos = self.pos()
        directiveKey = False
        for key in self._directiveEndEaters.keys():
            if self.find(key, pos) == pos:
                directiveKey = key
                break
        if not directiveKey:
            raise ParseError(self, msg='Invalid end directive')
                    
        self._directiveEndEaters[directiveKey](
            isLineClearToStartToken=isLineClearToStartToken)


    def _eatToThisEndDirective(self, directiveKey):
        finalPos = endRawPos = startPos = self.pos()
        directiveChar = self.setting('directiveStartToken')[0]
        isLineClearToStartToken = False
        while not self.atEnd():
            if self.peek() == directiveChar:
                if self.matchDirective() == 'end':
                    endRawPos = self.pos()
                    self.getDirectiveStartToken()
                    self.advance(3)                 # to end of 'end'
                    self.getWhiteSpace()
                    if self.startswith(directiveKey):
                        if self.isLineClearToStartToken(endRawPos):
                            isLineClearToStartToken = True
                            endRawPos = self.findBOL(endRawPos)
                        self.advance(len(directiveKey)) # to end of directiveKey
                        self.getWhiteSpace()
                        finalPos = self.pos()
                        break
            self.advance()
            finalPos = endRawPos = self.pos()

        textEaten = self.readTo(endRawPos, start=startPos)
        self.setPos(finalPos)
        
        endOfFirstLinePos = self.findEOL()
        
        if self.matchDirectiveEndToken():
            self.getDirectiveEndToken()
        elif isLineClearToStartToken and (not self.atEnd()) and self.peek() in '\r\n':
            self.readToEOL(gobble=True)
            
        if isLineClearToStartToken and self.pos() > endOfFirstLinePos:
            self._compiler.handleWSBeforeDirective()
        return textEaten

    def eatSimpleExprDirective(self):
        # filtered 
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLine = self.findEOL()
        self.getDirectiveStartToken()

        startPos = self.pos()
        expr = self.getExpression().strip()
        directiveKey = expr.split()[0]
        self._applyExpressionFilters(expr, directiveKey, startPos=startPos)
        if directiveKey in self._indentingDirectives:
            self.pushToIndentStack(directiveKey)
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLine)
        return expr

    ## specific directive eat methods
    
    def eatBreakPoint(self):
        self.setBreakPoint(self.pos())

    def eatShbang(self):
        # filtered 
        self.getDirectiveStartToken()
        self.advance(len('shBang'))
        self.getWhiteSpace()
        startPos = self.pos()
        shBang = self.readToEOL()
        self._applyExpressionFilters(shBang, 'shbang', startPos=startPos)
        self._compiler.setShBang(shBang.strip())

    def eatEncoding(self):
        # filtered 
        self.getDirectiveStartToken()
        self.advance(len('encoding'))
        self.getWhiteSpace()
        startPos = self.pos()
        encoding = self.readToEOL()
        self._applyExpressionFilters(encoding, 'encoding', startPos=startPos)               
        self._compiler.setModuleEncoding(encoding.strip())
        
    def eatCompiler(self):
        # filtered 
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLine = self.findEOL()
        startPos = self.pos()
        self.getDirectiveStartToken()
        self.advance(len('compiler'))   # to end of 'compiler'
        self.getWhiteSpace()

        startPos = self.pos()
        settingName = self.getIdentifier()

        if settingName.lower() == 'reset':
            self.getExpression() # gobble whitespace & junk
            self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLine)
            self._initializeSettings()
            self.configureParser()
            return
        
        self.getWhiteSpace()
        if self.peek() == '=':
            self.advance()
        else:
            raise ParserError(self)
        valueExpr = self.getExpression()
        endPos = self.pos()

        self._applyExpressionFilters('%s=%r'%(settingName, valueExpr),
                                     'compiler', startPos=startPos)
        
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLine)
        try:
            self._compiler.setCompilerSetting(settingName, valueExpr)
        except:
            out = sys.stderr
            print >> out, 'An error occurred while processing the following #compiler directive.'
            print >> out, '-'*80
            print >> out, self[startPos:endPos]
            print >> out, '-'*80
            print >> out, 'Please check the syntax of these settings.'
            print >> out, 'A full Python exception traceback follows.'
            raise


    def eatCompilerSettings(self):
        # filtered         
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLine = self.findEOL()
        self.getDirectiveStartToken()
        self.advance(len('compiler-settings'))   # to end of 'settings'
        
        keywords = self.getTargetVarsList()
        self.getExpression()            # gobble any garbage
            
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLine)

        if 'reset' in keywords:
            self._compiler._initializeSettings()
            self.configureParser()
            # @@TR: this implies a single-line #compiler-settings directive, and
            # thus we should parse forward for an end directive.
            # Subject to change in the future
            return 
        startPos = self.pos()
        settingsStr = self._eatToThisEndDirective('compiler-settings')            
        self._applyExpressionFilters(settingsStr, 'compilerSettings', startPos=startPos)
        try:
            self._compiler.setCompilerSettings(keywords=keywords, settingsStr=settingsStr)
        except:
            out = sys.stderr
            print >> out, 'An error occurred while processing the following compiler settings.'
            print >> out, '-'*80
            print >> out, settingsStr.strip()
            print >> out, '-'*80
            print >> out, 'Please check the syntax of these settings.'
            print >> out, 'A full Python exception traceback follows.'
            raise

    def eatAttr(self):
        # filtered         
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLinePos = self.findEOL()
        startPos = self.pos()
        self.getDirectiveStartToken()
        self.advance(len('attr'))
        self.getWhiteSpace()
        startPos = self.pos()
        if self.matchCheetahVarStart():
            self.getCheetahVarStartToken()
        attribName = self.getIdentifier()
        self.getWhiteSpace()
        self.getAssignmentOperator()
        expr = self.getExpression()
        self._applyExpressionFilters(expr, 'attr', startPos=startPos)
        self._compiler.addAttribute(attribName, expr)
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)

    def eatDef(self):
        # filtered         
        self._eatDefOrBlock('def')

    def eatBlock(self):
        # filtered
        startPos = self.pos()
        methodName, rawSignature = self._eatDefOrBlock('block')
        self._compiler._blockMetaData[methodName] = {
            'raw':rawSignature,
            'lineCol':self.getRowCol(startPos),
            }

    def eatClosure(self):
        # filtered         
        self._eatDefOrBlock('closure')
        
    def _eatDefOrBlock(self, directiveKey):
        # filtered 
        assert directiveKey in ('def','block','closure')
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLinePos = self.findEOL()
        startPos = self.pos()
        self.getDirectiveStartToken()
        self.advance(len(directiveKey))
        self.getWhiteSpace()
        if self.matchCheetahVarStart():
            self.getCheetahVarStartToken()
        methodName = self.getIdentifier()
        self.getWhiteSpace()
        if self.peek() == '(':
            argsList = self.getDefArgList()
            self.advance()              # past the closing ')'
            if argsList and argsList[0][0] == 'self':
                del argsList[0]
        else:
            argsList=[]

        def includeBlockMarkers():
            if self.setting('includeBlockMarkers'):
                startMarker = self.setting('blockMarkerStart')
                self._compiler.addStrConst(startMarker[0] + methodName + startMarker[1])

        self._applyExpressionFilters(self[startPos:self.pos()], 'def', startPos=startPos)

        if (self.peek()==':'
            and (self.setting('allowEmptySingleLineMethods')
                 or self[self.pos()+1:self.findEOL()].strip())):
            # single-line version
            isNestedDef = (self.setting('allowNestedDefScopes')
                           and [name for name in self._indentStack if name=='def'])
            self.getc()
            rawSignature = self[startPos:endOfFirstLinePos]
            self._eatSingleLineDef(directiveKey=directiveKey,
                                   methodName=methodName,
                                   argsList=argsList,
                                   startPos=startPos,
                                   endPos=endOfFirstLinePos)
            if directiveKey == 'def' and not isNestedDef:
                #@@TR: must come before _eatRestOfDirectiveTag ... for some reason
                self._compiler.closeDef()
            elif directiveKey == 'block':
                includeBlockMarkers()
                self._compiler.closeBlock()
            elif directiveKey == 'closure' or isNestedDef:
                self._compiler.dedent()
                
            self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
        else:
            if self.peek()==':':
                self.getc()            
            self.pushToIndentStack(directiveKey)
            rawSignature = self[startPos:self.pos()]
            self._eatMultiLineDef(directiveKey=directiveKey,
                                  methodName=methodName,
                                  argsList=argsList,
                                  startPos=startPos,
                                  isLineClearToStartToken=isLineClearToStartToken)
            if directiveKey == 'block':
                includeBlockMarkers()

        return methodName, rawSignature

    def _eatMultiLineDef(self, directiveKey, methodName, argsList, startPos,
                         isLineClearToStartToken=False):
        # filtered in calling method
        self.getExpression()            # slurp up any garbage left at the end
        signature = self[startPos:self.pos()]
        endOfFirstLinePos = self.findEOL()
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
        parserComment = ('Generated from ' + signature + 
                         ' at line %s, col %s' % self.getRowCol(startPos)
                         + '.')

        isNestedDef = (self.setting('allowNestedDefScopes')
                       and len([name for name in self._indentStack if name=='def'])>1)
        if directiveKey=='block' or (directiveKey=='def' and not isNestedDef):
            self._compiler.startMethodDef(methodName, argsList, parserComment)
        else: #closure
            self._useSearchList_orig = self.setting('useSearchList')
            self.setSetting('useSearchList', False)
            self._compiler.addClosure(methodName, argsList, parserComment)

        return methodName
            
    def _eatSingleLineDef(self, directiveKey, methodName, argsList, startPos, endPos):
        # filtered in calling method        
        origPos = self.pos()
        methodSrc = self[self.pos():endPos].strip()
        fullSignature = self[startPos:endPos]
        origBP = self.breakPoint()
        origSrc = self.src()
        self._src = methodSrc
        self.setPos(0)
        self.setBreakPoint(len(methodSrc))
        parserComment = ('Generated from ' + fullSignature + 
                         ' at line %s, col %s' % self.getRowCol(startPos)
                         + '.')
        isNestedDef = (self.setting('allowNestedDefScopes')
                       and [name for name in self._indentStack if name=='def'])
        if directiveKey=='block' or (directiveKey=='def' and not isNestedDef):
            self._compiler.startMethodDef(methodName, argsList, parserComment)
        else: #closure
            # @@TR: temporary hack of useSearchList
            useSearchList_orig = self.setting('useSearchList')
            self.setSetting('useSearchList', False)
            
            self._compiler.addClosure(methodName, argsList, parserComment)            

        self.parse(assertEmptyStack=False)
        if directiveKey=='closure' or isNestedDef: # @@TR: temporary hack of useSearchList
            self.setSetting('useSearchList', useSearchList_orig)
            
        self.getWhiteSpace()
        self._src = origSrc
        self.setBreakPoint(origBP) 
        self.setPos(endPos)

    def _X_eatSingleLineDef(self, methodName, argsList, startPos, endPos):
        ## @@TR: this is an experimental version of the method
        # filtered in calling method        
        origPos = self.pos()
        methodSrc = self[self.pos():endPos].strip()
        fullSignature = self[startPos:endPos]
        origBP = self.breakPoint()
        self.setBreakPoint(endPos-1)
        parserComment = ('Generated from ' + fullSignature + 
                         ' at line %s, col %s' % self.getRowCol(startPos)
                         + '.')
        self._compiler.startMethodDef(methodName, argsList, parserComment)
        #self.getWhiteSpace()
        self.parse(assertEmptyStack=False, endPos=endPos)
        #print self.pos()
        self.setBreakPoint(origBP) 
        return fullSignature # used by the #block code        
            
    def eatImport(self):
        # filtered
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLine = self.findEOL()
        self.getDirectiveStartToken()
        startPos = self.pos()
        impStatement = self.getExpression()
        self._applyExpressionFilters(impStatement, 'import', startPos=startPos)
        self._compiler.addImportStatement(impStatement)
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLine)
    
    def eatExtends(self):
        # filtered
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLine = self.findEOL()
        self.getDirectiveStartToken()
        self.advance(len('extends'))
        self.getWhiteSpace()
        startPos = self.pos()
        if self.setting('allowExpressionsInExtendsDirective'):
            baseName = self.getExpression()
        else:
            baseName = self.getDottedName()
                       
        self._applyExpressionFilters(baseName, 'extends', startPos=startPos)
        self._compiler.setBaseClass(baseName) # in compiler
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLine)
            
    def eatImplements(self):
        # filtered
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLine = self.findEOL()
        self.getDirectiveStartToken()
        self.advance(len('implements'))
        self.getWhiteSpace()
        startPos = self.pos()
        methodName = self.getIdentifier()
        self._applyExpressionFilters(methodName, 'implements', startPos=startPos)
        self._compiler.setMainMethodName(methodName)
        self.getExpression()  # throw away and unwanted crap that got added in
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLine)

    def eatSilent(self):
        # filtered
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLinePos = self.findEOL()
        self.getDirectiveStartToken()
        self.advance(len('silent'))
        self.getWhiteSpace()
        startPos = self.pos()
        expr = self.getExpression()
        self._applyExpressionFilters(expr, 'silent', startPos=startPos)        
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
        self._compiler.addSilent(expr)

    def eatEcho(self):
        # filtered
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLinePos = self.findEOL()
        self.getDirectiveStartToken()
        self.advance(len('echo'))
        self.getWhiteSpace()
        startPos = self.pos()
        expr = self.getExpression()
        self._applyExpressionFilters(expr, 'echo', startPos=startPos)        
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
        self._compiler.addEcho(expr, rawExpr=expr)

    def eatSet(self):
        # filtered
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLine = self.findEOL()
        self.getDirectiveStartToken()
        self.advance(3)
        self.getWhiteSpace()
        style = SET_LOCAL
        if self.startswith('local'):
            self.getIdentifier()
            self.getWhiteSpace()
        elif self.startswith('global'):
            self.getIdentifier()
            self.getWhiteSpace()
            style = SET_GLOBAL
        elif self.startswith('module'):
            self.getIdentifier()
            self.getWhiteSpace()
            style = SET_MODULE

        startsWithDollar = self.matchCheetahVarStart()
        startPos = self.pos()
        LVALUE = self.getCheetahVar(plain=True, skipStartToken=(not startsWithDollar))
        self.getWhiteSpace()
        OP = self.getAssignmentOperator()
        RVALUE = self.getExpression()        
        self._applyExpressionFilters(
            self[startPos+(startsWithDollar and 1 or 0):self.pos()],
            'set', startPos=startPos)
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLine)

        self._compiler.addSet(LVALUE, OP, RVALUE, style)
    
    def eatSlurp(self):
        if self.isLineClearToStartToken():
            self._compiler.handleWSBeforeDirective()
        self._compiler.commitStrConst()
        self.readToEOL(gobble=True)

    def eatEOLSlurpToken(self):
        if self.isLineClearToStartToken():
            self._compiler.handleWSBeforeDirective()
        self._compiler.commitStrConst()
        self.readToEOL(gobble=True)        

    def eatRaw(self):
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLinePos = self.findEOL()
        self.getDirectiveStartToken()
        self.getExpression()
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
        rawBlock = self._eatToThisEndDirective('raw')
        self._compiler.addRawText(rawBlock)
    
    def eatInclude(self):
        # filtered
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLinePos = self.findEOL()
        self.getDirectiveStartToken()
        self.advance(len('include'))

        self.getWhiteSpace()
        includeFrom = 'file'
        isRaw = False
        if self.startswith('raw'):
            self.advance(3)
            isRaw=True
            
        self.getWhiteSpace()            
        if self.startswith('source'):
            self.advance(len('source'))
            includeFrom = 'str'
            self.getWhiteSpace()
            if not self.peek() == '=':
                raise ParseError(self)
            self.advance()
        startPos = self.pos()
        sourceExpr = self.getExpression()
        self._applyExpressionFilters(sourceExpr, 'include', startPos=startPos)        
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
        self._compiler.addInclude(sourceExpr, includeFrom, isRaw)

    def eatCache(self):
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLinePos = self.findEOL()
        lineCol = self.getRowCol()
        self.getDirectiveStartToken()
        self.advance(len('cache'))
        startPos = self.pos()
        argList = self.getDefArgList(useNameMapper=True)
        self._applyExpressionFilters(argList, 'cache', startPos=startPos)
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
        cacheInfo = self._compiler.genCacheInfoFromArgList(argList)
        self._compiler.startCacheRegion(cacheInfo, lineCol)

    def eatCall(self):
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLinePos = self.findEOL()
        lineCol = self.getRowCol()
        self.getDirectiveStartToken()
        self.advance(len('call'))
        startPos = self.pos()
        
        useAutocallingOrig = self.setting('useAutocalling')
        self.setSetting('useAutocalling', False)
        self.getWhiteSpace()
        if self.matchCheetahVarStart():
            functionName = self.getCheetahVar()
        else:
            functionName = self.getCheetahVar(plain=True, skipStartToken=True)
        self.getWhiteSpace()
        args = self.getExpression().strip()
        
        self.setSetting('useAutocalling', useAutocallingOrig)
        
        self._applyExpressionFilters(self[startPos:self.pos()], 'call', startPos=startPos)
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)        
        self._compiler.startCallRegion(functionName, args, lineCol)

    def eatCallArg(self):
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLinePos = self.findEOL()
        lineCol = self.getRowCol()
        self.getDirectiveStartToken()

        self.advance(len('arg'))
        startPos = self.pos()
        self.getWhiteSpace()
        argName = self.getIdentifier()
        self.getWhiteSpace()
        self._applyExpressionFilters(argName, 'arg', startPos=startPos)
        self._compiler.setCallArg(argName, lineCol)
        if self.peek() == ':':
            self.getc()
        else:        
            self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)

    def eatFilter(self):
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLinePos = self.findEOL()

        self.getDirectiveStartToken()
        self.advance(len('filter'))
        self.getWhiteSpace()
        startPos = self.pos()
        if self.matchCheetahVarStart():
            isKlass = True
            theFilter = self.getExpression()
        else:
            isKlass = False
            theFilter = self.getIdentifier()
            self.getWhiteSpace()
        self._applyExpressionFilters(theFilter, 'filter', startPos=startPos)
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
        self._compiler.setFilter(theFilter, isKlass)
        
    def eatErrorCatcher(self):
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLinePos = self.findEOL()
        self.getDirectiveStartToken()
        self.advance(len('errorCatcher'))
        self.getWhiteSpace()
        startPos = self.pos()
        errorCatcherName = self.getIdentifier()
        self._applyExpressionFilters(errorCatcherName, 'errorcatcher', startPos=startPos)
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)        
        self._compiler.setErrorCatcher(errorCatcherName)
        
    def eatWhile(self):
        self._compiler.addWhile(self.eatSimpleExprDirective())

    def eatFor(self):
        # filtered 
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLinePos = self.findEOL()

        self.getDirectiveStartToken()
        startPos = self.pos()
        expr = self.getExpression()
        self._applyExpressionFilters(expr, 'for', startPos=startPos)        
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
        self.pushToIndentStack("for")
        self._compiler.addFor(expr)
        
    def eatIf(self):
        # filtered 
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLine = self.findEOL()
        self.getDirectiveStartToken()
        startPos = self.pos()
        expressionParts = self.getExpressionParts(pyTokensToBreakAt=[':'])
        if not self.atEnd() and self.peek()==':':
            pass
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLine)

        expr = ''.join(expressionParts).strip()
        self._applyExpressionFilters(expr, 'if', startPos=startPos)

        isTerniaryExpr = ('then' in expressionParts and 'else' in expressionParts)
        if isTerniaryExpr:
            conditionExpr = []
            trueExpr = []
            falseExpr = []
            currentExpr = conditionExpr
            for part in expressionParts:
                if part.strip()=='then':
                    currentExpr = trueExpr
                elif part.strip()=='else':
                    currentExpr = falseExpr
                else:
                    currentExpr.append(part)
                    
            conditionExpr = ''.join(conditionExpr)
            trueExpr = ''.join(trueExpr)
            falseExpr = ''.join(falseExpr)
            self._compiler.addOneLineIf(conditionExpr, trueExpr, falseExpr)
        else:
            self.pushToIndentStack('if')
            self._compiler.addIf(expr)
    
    def eatElse(self):
        """else, elif and else if"""
        # filtered 
        self._compiler.addElse(self.eatSimpleExprDirective())

    def eatTry(self):
        # filtered
        self._compiler.addTry(self.eatSimpleExprDirective())

    def eatExcept(self):
        # filtered
        self._compiler.addExcept(self.eatSimpleExprDirective())

    def eatFinally(self):
        # filtered
        self._compiler.addFinally(self.eatSimpleExprDirective())

    def eatPass(self):
        # filtered 
        self._compiler.addPass(self.eatSimpleExprDirective())

    def eatDel(self):
        # filtered 
        self._compiler.addDel(self.eatSimpleExprDirective())

    def eatAssert(self):
        # filtered 
        self._compiler.addAssert(self.eatSimpleExprDirective())

    def eatRaise(self):
        # filtered 
        self._compiler.addRaise(self.eatSimpleExprDirective())

    def eatBreak(self):
        # filtered 
        self._compiler.addBreak(self.eatSimpleExprDirective())

    def eatContinue(self):
        # filtered 
        self._compiler.addContinue(self.eatSimpleExprDirective())

    def eatStop(self):
        # filtered 
        self._compiler.addStop(self.eatSimpleExprDirective())

    def eatReturn(self):
        # filtered 
        self._compiler.addReturn(self.eatSimpleExprDirective())

    def eatRepeat(self):
        # filtered 
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLinePos = self.findEOL()
        self.getDirectiveStartToken()
        self.advance(len('repeat'))
        startPos = self.pos()
        expr = self.getExpression()
        self._applyExpressionFilters(expr, 'repeat', startPos=startPos)
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
        self.pushToIndentStack("repeat")
        self._compiler.addRepeat(expr)


    def eatUnless(self):
        # filtered 
        isLineClearToStartToken = self.isLineClearToStartToken()
        endOfFirstLinePos = self.findEOL()
        self.getDirectiveStartToken()
        self.advance(len('unless'))
        startPos = self.pos()
        expr = self.getExpression()
        self._applyExpressionFilters(expr, 'unless', startPos=startPos)
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
        self.pushToIndentStack("unless")
        self._compiler.addUnless(expr)

        
    ## end directive eaters

    def eatEndDef(self, isLineClearToStartToken=False):
        endOfFirstLinePos = self.findEOL()
        self.getExpression()
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)


        self.popFromIndentStack("def")
        isNestedDef = (self.setting('allowNestedDefScopes')
                       and [name for name in self._indentStack if name=='def'])
        if not isNestedDef:
            self._compiler.closeDef()
        else:
            # @@TR: temporary hack of useSearchList
            self.setSetting('useSearchList', self._useSearchList_orig)                    
            self._compiler.commitStrConst()
            self._compiler.dedent()
        
    def eatEndBlock(self, isLineClearToStartToken=False):
        endOfFirstLinePos = self.findEOL()
        self.getExpression()
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
        self.popFromIndentStack("block")
        self._compiler.closeBlock()

    def eatEndClosure(self, isLineClearToStartToken=False):
        self.eatDedentDirective('closure', isLineClearToStartToken)
        # @@TR: temporary hack of useSearchList
        self.setSetting('useSearchList', self._useSearchList_orig)        

    def eatEndCache(self, isLineClearToStartToken=False):
        endOfFirstLinePos = self.findEOL()
        self.getExpression()
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
        self._compiler.endCacheRegion()

    def eatEndCall(self, isLineClearToStartToken=False):
        endOfFirstLinePos = self.findEOL()
        self.getExpression()
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
        self._compiler.endCallRegion()

    def eatEndFilter(self, isLineClearToStartToken=False):
        endOfFirstLinePos = self.findEOL()
        self.getExpression()
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
        self._compiler.closeFilterBlock()

    def eatEndErrorCatcher(self, isLineClearToStartToken=False):
        endOfFirstLinePos = self.findEOL()
        self.getExpression()
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
        self._compiler.turnErrorCatcherOff()

    def eatDedentDirective(self, directiveKey, isLineClearToStartToken=False):        
        endOfFirstLinePos = self.findEOL()
        self.getExpression()
        self._eatRestOfDirectiveTag(isLineClearToStartToken, endOfFirstLinePos)
        self._compiler.commitStrConst()
        self._compiler.dedent()
        assert directiveKey in self._indentingDirectives
        self.popFromIndentStack(directiveKey)

    def eatEndWhile(self, isLineClearToStartToken=False):
        self.eatDedentDirective('while', isLineClearToStartToken)

    def eatEndIf(self, isLineClearToStartToken=False):
        self.eatDedentDirective('if', isLineClearToStartToken)

    def eatEndFor(self, isLineClearToStartToken=False):
        self.eatDedentDirective('for', isLineClearToStartToken)

    def eatEndTry(self, isLineClearToStartToken=False):
        self.eatDedentDirective('try', isLineClearToStartToken)

    def eatEndRepeat(self, isLineClearToStartToken=False):
        self.eatDedentDirective('repeat', isLineClearToStartToken)

    def eatEndUnless(self, isLineClearToStartToken=False):
        self.eatDedentDirective('unless', isLineClearToStartToken)

    ###

    def pushToIndentStack(self, directiveKey):
        assert directiveKey in self._indentingDirectives
        self._indentStack.append(directiveKey)

    def popFromIndentStack(self, directiveKey):
        if not self._indentStack:
            raise ParseError(self, msg="#end found, but nothing to end")
        
        if self._indentStack[-1] == directiveKey:
            del self._indentStack[-1]
        else:
            raise ParseError(self, msg="#end %s found, expected #end %s" %(
                directiveKey, self._indentStack[-1]))

    def assertEmptyIndentStack(self):
        if self._indentStack:
            errorMsg = (
                "Some #directives are missing their corresponding #end ___ tag: %s" %(
                ', '.join(self._indentStack)))
            raise ParseError(self, msg=errorMsg)


##################################################
## Make an alias to export
    
Parser = _HighLevelParser
