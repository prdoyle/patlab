#! /usr/bin/python -i

# Patrick Doyle  Jun 6 2012
#
# Patlab offers no fudge factor.  Patches much match precisely (like a stack of
# patches from patman).
#
# If you have approximate patches, you probably want to clean them up with
# patman or the diff/patch commands before fiddling with them in patlab.

from sys import stdin, stdout, stderr
import cStringIO
import re
import fnmatch
import subprocess
from tempfile import mkstemp
import os
import optparse # Python <2.7 doesn't have argparse

def debug( tag, *args ):
	if True: #not tag in [ "HWC" ]:
		return
	if len(args) >= 2:
		string = args[0] % args[1:]
	else:
		[ string ] = args
	stderr.write( string + "\n" )

path_strip_level = 1

def _stripped_path( path ):
	i = 0
	for _ in xrange( path_strip_level ):
		i = 1 + path.find( '/', i )
	return path[ i: ]

class PatlabError: pass

class PatlabErrorWithMessage( PatlabError ):
	def __repr__( self ):
		return "%s( \"%s\" )" % ( self.__class__.__name__, self.message )

class ParseError( PatlabErrorWithMessage ):
	def __init__( self, message ):
		self.message = message

# These errors should not happen when operating on whole patches.
# They will occur when operating on smaller pieces of a patch.
class MismatchedFilenameError( PatlabError ): pass # Diff error
class DisjointHunkError( PatlabError ): pass # Hunk error

class UnsupportedLineError( PatlabErrorWithMessage ):
	def __init__( self, line ):
		self.message = line

# The rest can occur when operating on whole patches
class AmbiguousLineNumberError( PatlabError ): pass
class ChangeToSameLineError( PatlabError ):
	def __init__( self, left_hunk, right_hunk, left_line, right_line ):
		self.left_hunk  = left_hunk
		self.right_hunk = right_hunk
		self.left_line  = left_line
		self.right_line = right_line

	def __repr__( self ):
		return "%s:\n   %s\n   %s" % ( self.__class__.__name__, repr(self.left_line), repr(self.right_line) )

class IncompatibleChangeToSameLineError( ChangeToSameLineError ):
	def __init__( self, left_hunk, right_hunk, left_line, right_line ):
		ChangeToSameLineError.__init__( self, left_hunk, right_hunk, left_line, right_line )

class IncompatibleFileRenameError( PatlabError ):
	def __init__( self, left_diff, right_diff ):
		self.left_diff = left_diff
		self.right_diff = right_diff

	def __repr__( self ):
		return "IncompatibleChangeToSameLineError:\n   %s\n   %s" % ( repr(self.left_line), repr(self.right_line) )

class UIObject:

	def girth( self ):
		io = cStringIO.StringIO()
		self.write_girth_to( io )
		result = io.getvalue()
		io.close()
		return result

	def headline( self ):
		io = cStringIO.StringIO()
		self.write_headline_to( io )
		result = io.getvalue()
		io.close()
		return result

	def abstract( self ):
		io = cStringIO.StringIO()
		self.write_abstract_to( io )
		result = io.getvalue()
		io.close()
		return result

	def contents( self ):
		io = cStringIO.StringIO()
		self.write_contents_to( io )
		result = io.getvalue()
		io.close()
		return result

	def girth_values( self ):
		return [( self.__class__.__name__, 1 )]

	def __repr__( self ): return self.abstract()
	def __str__( self ):  return self.contents()

class Algebraic:
	# For convenience, the symbolic operators return shrinkwrapped results.
	# If you want the raw results, use the ordinary method names.

	# First, a few handy synonyms

	def without  ( self, other ):  return self.compose( other.inverse() )
	def under    ( self, other ):  return other.compose( self ).without( other.over( self ) )

	# The Algebraic operators

	def __neg__( self ):             return self.inverse().shrinkwrapped()
	def __add__( self, other ):      return self.compose( other ).shrinkwrapped()
	def __sub__( self, other ):      return self.without( other ).shrinkwrapped()
	def __rshift__( self, other ):   return self.over( other ).shrinkwrapped()
	def __lshift__( self, other ):   return self.under( other ).shrinkwrapped()
	def __mod__( self, other ):      return self.conflicts( other ).shrinkwrapped()
	def __floordiv__( self, other ): return self.without_conflicts( other ).shrinkwrapped()
	#def __mul__( self, other ): return self.conflicts( other ).shrinkwrapped()

	def __xor__( self, lineno ): return self.split( lineno ) # Shrinkwrapping undoes splitting, so we don't shrinkwrap this one

class Enumerable:

	def write_girth_to( self, file ):
		sep = ''
		for ( name, value ) in self.girth_values()[1:]:
			file.write( "%s%d%s" % ( sep, value, name[0] ) )
			sep = ','

	def write_headline_to( self, file ):
		#file.write( "%s (%d)" % ( self.identifier(), len( self.elements() ) ) )
		file.write( "%s   (" % self.identifier() )
		self.write_girth_to( file )
		file.write( ")" )

	def write_abstract_to( self, file ):
		self.write_headline_to( file )
		for ( i, element ) in enumerate( self.elements() ):
			file.write( "\n  [%2d] " % i )
			element.write_headline_to( file )
		file.write( "\n" )

	def __getitem__( self, index ): return self.elements()[ index ]

	def girth_values( self ):
		[ my_girth ] = UIObject.girth_values( self )
		num_elements = len( self.elements() )
		if num_elements >= 1:
			def add_girths( left, right ):
				debug( "girth", "    add_girths( %s, %s )", left, right )
				if left:
					( ln, lv ) = left
				else:
					return right
				if right:
					( rn, rv ) = right
				else:
					return left
				assert( ln == rn )
				return ( ln, lv+rv )
			def dot_girths( left_girths, right_girths ):
				debug( "girth", "  dot_girths( %s, %s )", left_girths, right_girths )
				return map( add_girths, left_girths, right_girths )
			sub_girth = reduce( dot_girths, map( lambda e:e.girth_values(), self.elements() ) )
			return [ my_girth ] + sub_girth
		else:
			return [ my_girth ]

def _filter( predicate ):
	def f( p, x ):
		if p(x):
			debug( "_filter", "Match:    %r", x )
			return [ x, None ]
		else:
			debug( "_filter", "No match: %r", x )
			return [ None, x ]
	return lambda x: f(predicate,x)

def _any_line_matches( hunks, predicate ):
	for line in hunks.lines:
		if predicate( line ):
			return True
	return False

class Filter:

	def normalize( self, things ):
		return map( lambda t: t.normalize(), things )

class Piecewise_Filter( Filter ):

	def partition_elements( self, source, filter_func, targets ):
		[ yes, no ] = targets
		for e in source.elements():
			[ y, n ] = filter_func( e )
			if y:
				yes.elements().append( y )
			if n:
				no.elements().append( n )
		return targets

class Patches( Filter ):

	def __init__( self, predicate ):
		self.patch_filter = _filter( predicate )

	def partition_patch( self, patch ):
		return self.normalize( self.patch_filter( patch ) )

class Diffs( Piecewise_Filter ):

	def __init__( self, predicate ):
		self.diff_filter = _filter( predicate )

	def partition_patch( self, patch ):
		return self.normalize( self.partition_elements( patch,
			self.diff_filter, [ Patch( patch.name + ".matches" ), Patch( patch.name ) ] ) )

# These _fix functions re-establish the invariant that
#   h[i].rstart - h[i].lstart == h[i-1].rstop - h[i-1].lstop

def _fix_right_line_numbers( hunks ):
	offset = 0
	for h in hunks:
		h.rstart = h.lstart + offset
		h.normalize() # recompute stop lines
		offset = h.rstop - h.lstop

def _fix_left_line_numbers( hunks ):
	offset = 0
	for h in hunks:
		h.lstart = h.rstart + offset
		h.normalize() # recompute stop lines
		offset = h.lstop - h.rstop

class Hunks( Piecewise_Filter ):

	def __init__( self, predicate ):
		self.hunk_filter = _filter( predicate )

	def partition_patch( self, patch ):
		return self.partition_elements( patch, lambda d: self.partition_diff(d), [ Patch( patch.name + ".matches" ), Patch( patch.name ) ] )

	def partition_diff( self, diff ):
		[ yes, no ] = self.partition_elements( diff, self.hunk_filter, [ Diff( diff.lpath, diff.rpath ), Diff( diff.lpath, diff.rpath ) ] )
		# Assuming here that "yes" goes on top of "no"; that is, no+yes == diff.
		# Assuming "yes" goes on top of "no", then the left-line-numbers of no are ok,
		# and the right-line-numbers of yes are ok.  But the "middle" line number domain
		# will be all wrong because each patch is missing parts (which ended up in the
		# other patch) that could adjust line numbers.
		_fix_left_line_numbers ( no.hunks )
		_fix_right_line_numbers( yes.hunks )
		return self.normalize([ yes, no ])

class Hunks_With_Lines( Hunks ):

	def __init__( self, predicate ):
		self.hunk_filter = _filter( lambda h: _any_line_matches( h, predicate ) )

class Hunks_With_Conflicts( Hunks ):

	def __init__( self, hurdle_patch ):
		self.hurdle_patch = hurdle_patch # "hurdle_patch" is the patch you're trying to get over (get it?  ha ha)

	def partition_diff( self, diff ):
		debug( "HWC", "HWC diff:\n%s", diff )
		try:
			hurdle_diff = self.hurdle_patch.diffs_by_lname[ diff.rname ]
			debug( "HWC", "  Hurdle diff:\n%s", hurdle_diff )
		except KeyError:
			debug( "HWC", "  No corresponding diff -- no conflicts" )
			return [ None, diff ]
		try:
			# Fast path: if no conflicts, return early
			diff.over( hurdle_diff )
			# all is well
			debug( "HWC", "  Original diff has no conflicts" )
			return [ None, diff ]
		except ChangeToSameLineError:
			pass
		trial_diff = Diff( diff.lpath, diff.rpath )
		trial_diff.hunks = diff.hunks[:]
		trial_diff.normalize()
		conflict_diff = Diff( diff.rpath, diff.rpath )
		while trial_diff:
			try:
				# Attempt the operations we want to permit.
				# I think for now we just want the "over" operator.
				trial_diff.over( hurdle_diff )
				# Everything worked.  trial_diff is uncontroversial.
				debug( "HWC", "    Done removing conflicts" )
				conflict_diff.normalize()
				return [ conflict_diff, trial_diff ]
				#return [ diff.compose( trial_diff.inverse() ), trial_diff ]
			except ChangeToSameLineError, e:			
				# At least one hunk causes a conflict in at least one operation.
				# Remove it and try again.
				debug( "HWC", "    Removing conflicting hunk:\n%s", e.left_hunk )
				conflict_diff.hunks.append( e.left_hunk )
				trial_diff.hunks.remove( e.left_hunk )
				trial_diff.normalize()
				_fix_left_line_numbers( trial_diff.hunks ) # Want to keep the right-context fixed so it stays compatible with other


class Stack( Enumerable, UIObject ):

	def __init__( self, name ):
		self.patches = []
		self.name  = name

	def identifier( self ): return self.name
	def elements( self ): return self.patches

	def push( self, patch ):
		self.patches.insert( 0, patch )
		return patch

	def pop( self ):
		result = self.patches[ 0 ]
		del self.patches[ 0 ]
		return result

	def sink( self, *index ):
		try:
			[ start, end ] = index
			for i in range( start, end ):
				self.sink( i )
			return self
		except ValueError: # only 'start' specified
			[ index ] = index
			sinker  = self.patches[ index ]
			floater = self.patches[ index+1 ]
			up   = floater >> sinker
			down = sinker  << floater
			up.name   = floater.name
			down.name = sinker.name
			self.patches[ index:index+2 ] = [ up, down ]
			return self

	def float( self, *index ):
		try:
			[ start, end ] = index
			for i in range( start, end, -1 ):
				self.sink( i-1 )
			return self
		except ValueError: # only 'start' specified
			[ index ] = index
			return self.sink( index-1 )

	def squash( self, index ):
		self.patches[ index:index+2 ] = [ self.patches[ index+1 ] + self.patches[ index ] ]
		return self

	def filter( self, index, filter ):
		[ matching, non_matching ] = filter.partition_patch( self.patches[ index ] )
		self.patches[ index:index+1 ] = [ matching.shrinkwrapped(), non_matching.shrinkwrapped() ]
		return self

	def grep( self, index, regex ):
		return self.filter( index, Hunks_With_Lines( lambda l: re.search( regex, l.content ) ) )

	def glob( self, index, pattern ):
		return self.filter( index, Diffs( lambda d: fnmatch.filter( [ d.lname, d.rname ], pattern ) ) )

	def conflicts( self ):
		for f in range( len(self.patches)-1, 0, -1 ):
			floater = self.patches[ f ]
			for s in range( 0, f ):
				sinker = self.patches[ s ]
				try:
					floater = floater.over( sinker )
				except ChangeToSameLineError:
					print "%d * %d" % ( f, s )
					continue

	def sum( self ):
		return reduce( lambda a,b:a+b, reversed( self.patches ) )

	def _temp_patch_file( self, patch ):
		( patch_handle, patch_name ) = mkstemp( suffix="__"+patch.name.replace('/','_') )
		patch_file = os.fdopen( patch_handle, "w" )
		patch.write_contents_to( patch_file )
		patch_file.close()
		return patch_name

	def _edit1( self, patch ):
		file_name = self._temp_patch_file( patch )
		editor_command = "vim '%s'" % file_name
		print editor_command
		subprocess.call( editor_command, shell=True )
		patch2 = load( file_name )
		patch2.name = patch.name
		os.remove( file_name )
		return patch2

	def _edit2( self, patches ):
		[ upper, lower ] = patches
		upper_name = self._temp_patch_file( upper )
		lower_name = self._temp_patch_file( lower )
		editor_command = "vim -o '%s' '%s'" % ( upper_name, lower_name )
		print editor_command
		subprocess.call( editor_command, shell=True )
		( upper2, lower2 ) = ( load( upper_name ), load( lower_name ) )
		( upper2.name, lower2.name ) = ( upper.name, lower.name )
		for diff in upper2.diffs:
			_fix_left_line_numbers( diff.hunks )
		for diff in lower2.diffs:
			_fix_right_line_numbers( diff.hunks )
		os.remove( upper_name )
		os.remove( lower_name )
		return filter( None, [ upper2, lower2 ] )

	def edit( self, index ):
		self.patches[ index ] = self._edit1( self.patches[ index ] )
		return self

	def edit2( self, index ):
		self.patches[ index:index+2 ] = self._edit2( self.patches[ index:index+2 ] )
		return self

	def sift( self, index ):
		original = patches[ index ]
		patches[ index:index+1 ] = self._edit2([ Patch( original.name + ".upper" ), original ])
		return self

	def write_contents_to( self, file ):
		for patch in self.patches:
			patch.write_contents_to( file )

	def __len__( self ): return len( self.patches )

	def __delitem__( self, i ):    self.patches.__delitem__(i)
	def __setitem__( self, i, v ): self.patches.__setitem__(i,v)

def _compose_func( left, right ):
	if left:
		if right:
			return [ left.compose( right ) ]
		else:
			return [ left ]
	else:
		if right:
			return [ right ]
		else:
			return []

def _over_func( left, right ):
	if left:
		if right:
			return [ left.over( right ) ]
		else:
			return [ left ]
	else:
		return []

class Patch( Enumerable, UIObject, Algebraic ):

	def __init__( self, name ):
		self.diffs = []
		self.name  = name

	def normalize( self ):
		self.diffs.sort( lambda left, right: cmp( left.lpath, right.lpath ) )
		self.diffs_by_lname  = {}
		self.diffs_by_rname = {}
		for diff in self.diffs:
			self.diffs_by_lname [ diff.lname ] = diff
			self.diffs_by_rname [ diff.rname ] = diff
		return self

	def shrinkwrapped( self ):
		result = Patch( self.name )
		diffs = self.diffs
		diffs = map( lambda d: d.shrinkwrapped(), diffs )
		diffs = filter( lambda d: not d.is_identity(), diffs )
		result.diffs = diffs
		return result.normalize()

	def identifier( self ): return self.name
	def elements( self ): return self.diffs

	def inverse( self ):
		result = Patch( '-' + self.name )
		result.diffs = [ diff.inverse() for diff in self.diffs ]
		return result.normalize()

	def _combine( self, other, combine_name, combine_func, skip_incompatible=False ):
		result = Patch( "%s%s%s" % ( self.name, combine_name, other.name ) )
		all_names = set([ d.rname for d in self.diffs ] + [ d.lname for d in other.diffs ])
		debug( "combine", "Patch combine names: %s", all_names )
		for name in all_names:
			if name in self.diffs_by_rname:
				if name in other.diffs_by_lname:
					debug( "combine", "  Both have %s", name )
					left  = self .diffs_by_rname[ name ]
					right = other.diffs_by_lname[ name ]
					result.diffs.extend( combine_func( left, right ) )
				else:
					debug( "combine", "  Only self has %s", name )
					if name in other.diffs_by_rname:
						if skip_incompatible:
							pass
						else:	
							raise IncompatibleFileRenameError( self, other )
					else:
						result.diffs.extend( combine_func( self.diffs_by_rname[ name ], None ) )
			else:
				debug( "combine", "  Only other has %s", name )
				if name in self.diffs_by_lname:
					if skip_incompatible:
						pass
					else:
						raise IncompatibleFileRenameError( self, other )
				else:
					result.diffs.extend( combine_func( None, other.diffs_by_lname[ name ] ) )
		return result.normalize()

	def compose( self, other ):
		return self._combine( other, "+", _compose_func )

	def over( self, other ):
		return self._combine( other, ">>", _over_func )

	def _compute_conflicts( self, other ):
		return Hunks_With_Conflicts( other ).partition_patch( self )

	def conflicts( self, other ):
		return self._compute_conflicts( other )[ 0 ]

	def without_conflicts( self, other ):
		return self._compute_conflicts( other )[ 1 ]

	def split( self, line_number ):
		matching_diffs = [ d for d in self.diffs if d._hunk_with_left_line_number( line_number ) ]
		if len( matching_diffs ) != 1:
			raise AmbiguousLineNumberError
		else:
			matching_diff = matching_diffs[0]
		return self.split_diff( matching_diff, line_number )

	def split_diff( self, diff, line_number ):
		assert( diff in self.diffs )
		split_diff = diff.split( line_number )
		if split_diff == diff:
			return self
		else:
			result = Patch( "%s^%d" % ( self.name, line_number ) )
			diffs = self.diffs[:]
			diffs.remove( diff )
			diffs.append( split_diff )
			result.diffs = diffs
			return result.normalize()

	def write_contents_to( self, file ):
		for diff in self.diffs:
			diff.write_contents_to( file )

	def save_to( self, filename ):
		file = open( filename, "w" )
		self.write_contents_to( file )
		file.close()

	def __nonzero__( self ):
		if self.diffs:
			return True
		else:
			return False

class _Hunk_Line_Iterator:

	def __init__( self, hunk ):
		self.line_number1 = hunk.lstart
		self.line_number2 = hunk.rstart
		self.lines1 = hunk.matching_lines(['-',' '])
		self.lines2 = hunk.matching_lines(['+',' '])
		# We reverse the lists to make them into stacks on which we can use pop()
		self.lines1.reverse()
		self.lines2.reverse()

	def top_pair( self ):
		# Returns a pair of Lines that correspond to self.line_numberX.
		# At most one of the lines will be Null, if a line is to be inserted / deleted.
		# 
		debug( "top_pair", "top_pair @ %d, %d", self.line_number1, self.line_number2 );
		line1 = None
		line2 = None
		if self.lines1:
			line1 = self.lines1[-1]
		if self.lines2:
			line2 = self.lines2[-1]
		debug( "top_pair", "  initial line1: %s", line1 );
		debug( "top_pair", "  initial line2: %s", line2 );
		if line1 and line2:
			if line1.kind == '-' and line2.kind != '+':
				line2 = None
			elif line2.kind == '+' and line1.kind != '-':
				line1 = None
		debug( "top_pair", "  line1: %s", line1 );
		debug( "top_pair", "  line2: %s", line2 );
		return [ line1, line2 ]

	def pop_pair( self ):
		assert( self.more_to_go() )
		[ line1, line2 ] = self.top_pair()
		if line1:
			self.lines1.pop()
		if line2:
			self.lines2.pop()
		debug( "top_pair", "  popped line1: %s", line1 );
		debug( "top_pair", "  popped line2: %s", line2 );
		[ next1, next2 ] = self.top_pair()
		if next1:
			self.line_number1 += 1
		if next2:
			self.line_number2 += 1
		return [ line1, line2 ]

	def pop( self ):
		# Returns a list of lines suitable to be attached to the end of
		# a line list (as with extend()) that would reproduce the
		# original hunk, albeit possibly with the lines in a different
		# but equivalent order.
		#
		result = filter( None, self.pop_pair() )
		if len( result ) == 2:
			[ b,a ] = result
			if b == a: # [x,x] indicates the line is unchanged.  It should appear just once in the hunk.
				if a.is_both():
					return [a]
				else:
					return [Line( ' ', a.content )]
		return result

	def more_to_go( self ): return self.lines1 or self.lines2

	def __nonzero__( self ):
		if self.more_to_go():
			return True
		else:
			return False

class _Diff_Line_Iterator:

	def __init__( self, diff ):
		# We reverse the lists to make them into stacks on which we can use pop()
		self.hunks = diff.hunks[:]
		self.hunks.reverse()
		self._advance_hunk()

	def _advance_hunk( self ):
		if self.hunks:
			hunk = self.hunks.pop()
			self.current_hunk = hunk
			self.hunk_iter = _Hunk_Line_Iterator( hunk )
		else:
			self.current_hunk = None
			self.hunk_iter = None

	def line_number1( self ): return self.hunk_iter.line_number1
	def line_number2( self ): return self.hunk_iter.line_number2

	def line_numbers( self ): return [ self.line_number1(), self.line_number2() ]

	def top_pair( self ):
		if self.hunk_iter:
			return self.hunk_iter.top_pair()
		else:
			return [ None, None ]

	def pop_pair( self ):
		result = self.hunk_iter.pop_pair()
		if not self.hunk_iter.more_to_go():
			self._advance_hunk()
		return result

	def pop( self ):
		result = self.hunk_iter.pop()
		if not self.hunk_iter.more_to_go():
			self._advance_hunk()
		return result

	def more_to_go( self ):
		if self.current_hunk:
			assert( self.hunk_iter.more_to_go() )
			return True
		else:
			return False

	def __nonzero__( self ):
		return self.more_to_go()

def _unique_min( seq ):
	result = None
	least  = None
	for item in seq:
		if not least or item < least:
			least = item
			result = item
		elif result == item:
			result = None
	return result

def _by_left_line_info( left, right ): return left.lcmp( right )

def _is_always_increasing( items ):
	for (i,e) in enumerate( items[:-1] ):
		if e.lcmp( items[ i+1 ] ) != -1:
			return False
	return True

class Diff( Enumerable, UIObject, Algebraic ):

	def __init__( self, left_path, right_path ):
		self.hunks = []
		self.lpath = left_path
		self.rpath = right_path

	def _extend( self, debug_tag, line_numbers, extension ):
		debug( debug_tag, "  expected line numbers: %s", line_numbers )
		debug( debug_tag, "  extension: %s", extension )
		if self._append_hunk_if_gap( line_numbers ):
			debug( debug_tag, "  new hunk:\n%s---", self.hunks[-1] )
		self.hunks[-1].lines.extend( extension )
		debug( debug_tag, "  extended hunk:\n%s---", self.hunks[-1] )

	def normalize( self ):
		for hunk in self.hunks:
			hunk.normalize()
		self.hunks.sort( _by_left_line_info )
		assert( _is_always_increasing( self.hunks ) )
		self.lname = _stripped_path( self.lpath  )
		self.rname = _stripped_path( self.rpath )
		return self

	def shrinkwrapped( self ):
		result = Diff( self.lpath, self.rpath )
		hunks = self.hunks
		hunks = map( lambda h: h.shrinkwrapped(), hunks )
		hunks = filter( lambda h: not h.is_identity(), hunks )
		result.hunks = hunks
		return result.normalize()

	def identifier( self ): return self.lname
	def elements( self ):   return self.hunks

	def inverse( self ):
		result = Diff( self.rpath, self.lpath )
		result.hunks = [ hunk.inverse() for hunk in self.hunks ]
		return result.normalize()

	def _append_hunk_if_gap( self, expected_lines ):
		[ lnum,  rnum  ] = expected_lines
		[ lstop, rstop ] = self.hunks[-1].stop_line_numbers()
		assert( lstop-1 <= lnum )
		assert( rstop-1 <= rnum )
		if lstop < lnum or rstop < rnum:
			self.hunks.append( Hunk( lnum, rnum ) )
			return True
		else:
			return False

	def compose( self, other ):
		if self.rname != other.lname:
			raise MismatchedFilenameError
		elif other.is_identity():
			return self
		elif self.is_identity():
			return other

		# Line domains:
		# If self maps file f1 to f2, and other maps f2 to f3, then self+other maps f1 to f3.
		# That means self's left lines and other's right lines always have the right numbers.

		self_iter  = _Diff_Line_Iterator( self  )
		other_iter = _Diff_Line_Iterator( other )
		result = Diff( self.lpath, other.rpath )
		[ n1, n2 ] = self_iter .line_numbers()
		[ n3, n4 ] = other_iter.line_numbers()
		assert( n1 == n2 and n3 == n4 ) # Nothing has happened yet to put these out of sync
		if n2 < n3:
			result.hunks.append( Hunk( n1, n2 ) )
		elif n2 > n3:
			result.hunks.append( Hunk( n3, n4 ) )
		else:
			result.hunks.append( Hunk( n1, n4 ) )

		while self_iter and other_iter:
			( self_hunk, other_hunk ) = ( self_iter.current_hunk, other_iter.current_hunk )
			[ n1, n2 ] = self_iter .line_numbers()
			[ n3, n4 ] = other_iter.line_numbers()
			[ n5, n6 ] = result.hunks[-1].stop_line_numbers()
			offset = n6 - n5
			debug( "compose", "\nCOMPOSE -- self:%d|%d other:%d|%d result:%d|%d", n1, n2, n3, n4, n5, n6 )
			[ l1, l2 ] = self_iter .top_pair()
			[ l3, l4 ] = other_iter.top_pair()
			if n2 < n3:
				debug( "compose", "    case: Self is behind: %d < %d", self_iter.line_number2(), other_iter.line_number1() )
				expected_line_numbers = [ n1, n1 + offset ]
				extension = self_iter.pop()
			elif n2 > n3:
				debug( "compose", "    case: Other is behind: %d > %d", self_iter.line_number2(), other_iter.line_number1() )
				expected_line_numbers = [ n4 - offset, n4 ]
				extension = other_iter.pop()
			else: # Multiple changes to the same line
				assert( self_iter.more_to_go() )
				expected_line_numbers = [ n1, n4 ]
				self_iter .pop_pair()
				other_iter.pop_pair()
				if not ( l2 or l3 ):
					if l1 == l4:
						debug( "compose", "    case: Line is deleted then restored" )
						extension = [ Line(' ',l1.content) ]
					else:
						debug( "compose", "    case: Line is deleted and a different one inserted" )
						extension = [ l1, l4 ]
				elif l2 != l3:
					debug( "compose", "    case: WHOOPS! '%s' != '%s'", l2, l3 )
					raise IncompatibleChangeToSameLineError( self_hunk, other_hunk, l2, l3 )
				if not l1:
					if not l4:
						debug( "compose", "    case: Line is inserted then deleted" )
						extension = []
					else:
						debug( "compose", "    case: Line is inserted" )
						extension = [ Line('+',l4.content) ]
				elif not l4:
					debug( "compose", "    case: Line is deleted" )
					extension = [ Line('-',l1.content) ]
				elif l1 == l4:
					if l1.kind == ' ':
						assert( l1 == l2 )
						debug( "compose", "    case: Line is unchanged" )
					else:
						debug( "compose", "    case: Line is is changed then changed back" )
					extension = [ Line(' ',l4.content) ]
				else:
					debug( "compose", "    case: Line is changed" )
					extension = [ Line('-',l1.content), Line('+',l4.content) ]

			result._extend( "compose", expected_line_numbers, extension )

		debug( "compose", "  Adding suffix from %s", self_iter and "self" or other_iter and "other" or "nobody" )
		while self_iter:
			[ n1, n2 ] = self_iter .line_numbers()
			[ n5, n6 ] = result.hunks[-1].stop_line_numbers()
			offset = n6-n5
			expected_line_numbers = [ n1, n1 + offset ]
			extension = self_iter.pop()
			result._extend( "compose", expected_line_numbers, extension )
		while other_iter:
			[ n3, n4 ] = other_iter.line_numbers()
			[ n5, n6 ] = result.hunks[-1].stop_line_numbers()
			offset = n6-n5
			expected_line_numbers = [ n4 - offset, n4 ]
			extension = other_iter.pop()
			result._extend( "compose", expected_line_numbers, extension )
		debug( "compose", "  Result before normalization:\n%s", result )
		return result.normalize()

	def over( self, other ):
		if self.rname != other.lname:
			raise MismatchedFilenameError
		elif self.is_identity() or other.is_identity(): # Let's not break our brains over this case
			return self

		# Line domains:
		# Suppose self maps file f1 to f2, and other maps f2 to f3.
		# Define p1 = f2.under(f1) and p2 = f1.over(f2).  Then p1+p2 maps f1 to f3.
		# That means other's right line numbers equal the p2's right line numbers.
		# When other contains no line information, we can use left's right line numbers
		# offset by the accumulated insertions/deletions from other.

		self_iter  = _Diff_Line_Iterator( self  )
		other_iter = _Diff_Line_Iterator( other )
		result = Diff( self.lpath, other.rpath )
		[ n1, n2 ] = self_iter .line_numbers()
		[ n3, n4 ] = other_iter.line_numbers()
		assert( n1 == n2 and n3 == n4 ) # Nothing has happened yet to put these out of sync
		if n2 < n3:
			result.hunks.append( Hunk( n1, n2 ) )
		elif n2 > n3:
			result.hunks.append( Hunk( n3, n4 ) )
		else:
			result.hunks.append( Hunk( n1, n4 ) )

		while self_iter and other_iter:
			( self_hunk, other_hunk ) = ( self_iter.current_hunk, other_iter.current_hunk )
			[ n1, n2 ] = self_iter .line_numbers()
			[ n3, n4 ] = other_iter.line_numbers()
			[ n5, n6 ] = result.hunks[-1].stop_line_numbers()
			hunk_offset = n6 - n5
			other_accumulated_offset = n4 - n3
			debug( "over", "\nOVER -- self:%d|%d other:%d|%d result:%d|%d", n1, n2, n3, n4, n5, n6 )
			[ l1, l2 ] = self_iter .top_pair()
			[ l3, l4 ] = other_iter.top_pair()
			if n2 < n3:
				debug( "over", "    case: Self is behind: %d < %d", self_iter.line_number2(), other_iter.line_number1() )
				left = n1 + other_accumulated_offset
				expected_line_numbers = [ left, left + hunk_offset ]
				extension = self_iter.pop()
			elif n2 > n3:
				debug( "over", "    case: Other is behind: %d > %d", self_iter.line_number2(), other_iter.line_number1() )
				expected_line_numbers = [ n4 - hunk_offset, n4 ]
				[ l3, l4 ] = other_iter.pop_pair()
				if l4:
					extension = [ Line(' ',l4.content) ]
				else:
					extension = []
			else: # Multiple changes to the same line
				assert( self_iter.more_to_go() )
				expected_line_numbers = [ n4 - hunk_offset, n4 ]
				if l2 != l3:
					debug( "over", "  WHOOPS! '%s' != '%s'", l2, l3 )
					raise IncompatibleChangeToSameLineError( self_hunk, other_hunk, l2, l3 )
				elif l3 == l4:
					debug( "over", "  Line is unaffected by other" )
					extension = self_iter.pop()
					other_iter.pop_pair()
				elif l1 == l2:
					self_iter.pop()
					other_iter.pop_pair()
					if l4:
						debug( "over", "  Line is unaffected by self - use other for context" )
						extension = [ Line(' ',l4.content) ]
					else:
						debug( "over", "  Line is unaffected by self - omit context line" )
						extension = []
						if not result.hunks[-1].lines:
							# other has deleted the first line of result's context,
							# so omitting it means we have to adjust the result's
							# starting line numbers too.
							debug( "over", "    (Increment start line numbers)" )
							result.hunks[-1].lstart += 1
							result.hunks[-1].rstart += 1
				else:
					raise ChangeToSameLineError( self_hunk, other_hunk, l2, l3 )

			result._extend( "over", expected_line_numbers, extension )

		debug( "over", "  Adding suffix from %s", self_iter and "self" or other_iter and "other" or "nobody" )
		while self_iter:
			[ n1, n2 ] = self_iter .line_numbers()
			[ n5, n6 ] = result.hunks[-1].stop_line_numbers()
			hunk_offset = n6 - n5
			left = n1 + other_accumulated_offset
			expected_line_numbers = [ left, left + hunk_offset ]
			extension = self_iter.pop()
			result._extend( "over", expected_line_numbers, extension )
		while other_iter:
			[ n3, n4 ] = other_iter.line_numbers()
			[ n5, n6 ] = result.hunks[-1].stop_line_numbers()
			hunk_offset = n6-n5
			expected_line_numbers = [ n4 - hunk_offset, n4 ]
			[ l3, l4 ] = other_iter.pop_pair()
			if l4:
				extension = [ Line(' ',l4.content) ]
			else:
				extension = []
			result._extend( "over", expected_line_numbers, extension )
		debug( "over", "  Result before normalization:\n%s", result )
		return result.normalize()

	def _hunk_with_left_line_number( self, left_line_number ):
		matching_hunks = [ h for h in self.hunks if h.lcmp( left_line_number ) == 0 ]
		if matching_hunks:
			assert( len(matching_hunks) == 1 )
			return matching_hunks[0]
		else:
			return None

	def split( self, left_line_number ):
		matching_hunk = self._hunk_with_left_line_number( left_line_number )
		if not matching_hunk:
			return self

		result = Diff( self.lpath, self.rpath )
		result.hunks = self.hunks[:]

		iter = _Hunk_Line_Iterator( matching_hunk )
		top = Hunk( matching_hunk.lstart, matching_hunk.rstart )
		while iter.line_number1 < left_line_number:
			extension = iter.pop()
			debug( "split", "  top extension: %s", extension )
			top.lines.extend( extension )
		bottom = Hunk( iter.line_number1, iter.line_number2 )
		while iter.more_to_go():
			extension = iter.pop()
			debug( "split", "  bot extension: %s", extension )
			bottom.lines.extend( extension )

		result.hunks.remove( matching_hunk )
		result.hunks.append( top.normalize() )
		result.hunks.append( bottom.normalize() )
		return result.normalize()

	def write_contents_to( self, file ):
		file.write( "--- %s\n" % self.lpath  )
		file.write( "+++ %s\n" % self.rpath )
		for hunk in self.hunks:
			hunk.write_contents_to( file )

	def num_input_lines( self ):
		return 2 + sum([ hunk.num_input_lines() for hunk in self.hunks ])

	def is_identity( self ): return len( self.hunks ) == 0 # Note: before normalization, technically we'd have to check every hunk

def _range_cmp( start, stop, needle ):
	if needle < start:
		return 1
	elif stop <= needle:
		return -1
	else:
		return 0

class Hunk( Enumerable, UIObject, Algebraic ):

	def __init__( self, left_start_line_num, right_start_line_num ):
		self.lstart = left_start_line_num
		self.rstart = right_start_line_num
		self.lines = []
		self.meta_line_count = 1

	def _has_required_lines( self, l, r ):
		# Not recommended after normalizing.  Inefficient.
		return ( l == self.num_left_lines() ) and ( r == self.num_right_lines() )

	def _group_lines( self ):
		# Make runs of - and runs of + adjacent where possible, with - first
		buffer = []
		old_lines = self.lines
		new_lines = []
		for line in old_lines:
			kind = line.kind
			if kind == '-':
				new_lines.append( line )
			elif kind == '+':
				buffer.append( line )
			else:
				new_lines.extend( buffer )
				new_lines.append( line )
				buffer = []
		new_lines.extend( buffer )
		self.lines = new_lines

	def _trim_lines( self, lines, limit ):
		run_length = 0
		for i in xrange( len(lines) ):
			if lines[i].kind == ' ':
				run_length += 1
			else:
				break
		to_trim = run_length - limit
		if to_trim >= 1:
			del lines[ 0 : to_trim ]
			return to_trim
		else:
			return 0

	def _trim_context( self, limit ):
		lines = self.lines
		lines.reverse()
		self._trim_lines( lines, limit )
		lines.reverse()
		to_trim = self._trim_lines( lines, limit )
		self.lstart += to_trim
		self.rstart += to_trim

	def _shrinkwrap_lines( self ):
		iter = _Hunk_Line_Iterator( self )
		lines = []
		while iter:
			lines.extend( iter.pop() )
		self.lines = lines
		self._trim_context( 3 )

	def normalize( self ):
		self._group_lines()
		self.lstop = self.lstart + self.num_left_lines()
		self.rstop = self.rstart + self.num_right_lines()
		return self

	def shrinkwrapped( self ):
		result = Hunk( self.lstart, self.rstart )
		lines = self.lines
		lines = map( lambda x: x.shrinkwrapped(), lines )
		result.lines = lines
		result._shrinkwrap_lines()
		return result.normalize()

	def inverse( self ):
		result = Hunk( self.rstart, self.lstart )
		result.lines = [ line.inverse() for line in self.lines ]
		return result.normalize()

	def identifier( self ): return "@%d" % self.lstart
	def elements( self ):   return self.lines

	def lcmp( self, other ): # Compare line numbers using the left domain
		if isinstance( other, Hunk ):
			lcmp1 = self.lcmp( other.lstart )
			lcmp2 = self.lcmp( other.lstop )
			if lcmp1 == lcmp2:
				return lcmp1
			else:
				return 0 # Hunks overlap
		else:
			return _range_cmp( self.lstart, self.lstop, other )

	def rlcmp( self, other ): # Compare line numbers using self's right domain and other's left domain
		debug( "rlcmp", "rlcmp( %s, %s )", self, other )
		lcmp1 = other.lcmp( self.rstart )
		lcmp2 = other.lcmp( self.rstop-1 )
		debug( "rlcmp", "  lcmp1 = %d", lcmp1 )
		debug( "rlcmp", "  lcmp2 = %d", lcmp2 )
		if lcmp1 == lcmp2:
			result = lcmp1
		else:
			result = 0 # Hunks overlap
		debug( "rlcmp", "  returning %d", result )
		return result

	def write_contents_to( self, file ):
		file.write( "@@ -%d,%d +%d,%d @@\n" % ( self.lstart, self.num_left_lines(), self.rstart, self.num_right_lines() ) )
		for line in self.lines:
			line.write_contents_to( file )

	def num_input_lines( self ):
		return self.meta_line_count + len( self.lines )

	def matching_lines( self, kinds ):
		return [ line for line in self.lines if line.kind in kinds ]

	def num_left_lines( self ):  return len( self.matching_lines(['-',' ']) )
	def num_right_lines( self ): return len( self.matching_lines(['+',' ']) )
	def is_identity( self ):     return len( self.matching_lines([' ']) ) == len( self.lines )

	def stop_line_numbers( self ): return ( self.lstart + self.num_left_lines(), self.rstart + self.num_right_lines() )

	def __repr__( self ):  return self.contents()

class Line( UIObject ):

	def __init__( self, kind, content ):
		self.kind = kind
		self.content = content

	def normalize( self ): return self
	def shrinkwrapped( self ): return self

	def inverse( self ):
		opposite_kind = { '-':'+', '+':'-', ' ':' ' }[ self.kind ]
		return Line( opposite_kind, self.content )

	def is_left( self ):  return self.kind in ['-',' ']
	def is_right( self ): return self.kind in ['+',' ']
	def is_both( self ):  return self.kind == ' '

	def write_contents_to( self, file ):
		if self.content[-1] == '\n':
			file.write( "%s%s" % ( self.kind, self.content ) )
		else:
			file.write( "%s%s\n\\ No newline at end of file" % ( self.kind, self.content ) )

	def write_headline_to( self, file ):
		file.write( "%s%s" % ( self.kind, self.content.rstrip() ) )

	def write_abstract_to( self, file ): self.write_headline_to( file )

	def __eq__( self, other ):
		if isinstance( other, Line ):
			return self.content == other.content
		else:
			return False
	def __ne__( self, other ): return not self.__eq__( other )

	def __repr__( self ): return str( self ).rstrip()

def _line_from( string ):
	debug( "parse", "_line_from( \"%s\" )", string )
	try:
		kind = { '-':'-', '+':'+', ' ':' ' }[ string[ 0 ] ]
	except KeyError:
		raise UnsupportedLineError( string )
	content = string[ 1: ]
	return Line( kind, content ).normalize()

def _hunk_from( descriptor, line_content ):
	debug( "parse", "_hunk_from( \"%s\" )", descriptor )
	[ atat, left, right, atat2 ] = descriptor.split()
	( lstart, llen ) = left.split(',')
	( lstart, llen ) = ( -int(lstart), int(llen) )
	( rstart, rlen ) = right.split(',')
	( rstart, rlen ) = (  int(rstart), int(rlen) )
	result = Hunk( lstart, rstart )
	while not result._has_required_lines( llen, rlen ):
		line = line_content[0]
		line_content = line_content[1:]
		if line[ 0 ] == '\\': # Special indicator that newline is missing in previous line
			debug( "parse", "  Found a 'no newline' directive: %s", line )
			last_line = result.lines[-1]
			assert( last_line.content[-1] == '\n' )
			new_last_line = Line( last_line.kind, last_line.content[:-1] ).normalize()
			debug( "parse", "    - New last line is \"%s\"", new_last_line )
			result.lines[-1] = new_last_line
			result.meta_line_count += 1
		else:
			result.lines.append( _line_from( line ) )
	return result.normalize()

def _diff_from( left_descriptor, right_descriptor, hunk_content ):
	debug( "parse", "_diff_from( \"%s\", \"%s\" )", left_descriptor, right_descriptor )
	[ left_prefix,  lpath ] = left_descriptor.split( None, 2 )[ 0:2 ]
	[ right_prefix, rpath ] = right_descriptor.split( None, 2 )[ 0:2 ]
	if left_prefix != "---":
		raise ParseError( "Expected ---; found %s" % left_descriptor )
	if right_prefix != "+++":
		raise ParseError( "Expected +++; found %s" % right_descriptor )
	result = Diff( lpath, rpath )
	while hunk_content:
		hunk_descriptor = hunk_content[0]
		if hunk_descriptor[ 0:2 ] != "@@":
			break
		hunk = _hunk_from( hunk_descriptor, hunk_content[ 1: ] )
		result.hunks.append( hunk )
		hunk_content = hunk_content[ hunk.num_input_lines(): ]
	return result.normalize()

def _patch_from( name, diff_content ):
	result = Patch( name )
	while diff_content:
		left_descriptor  = diff_content[0]
		right_descriptor = diff_content[1]
		diff = _diff_from( left_descriptor, right_descriptor, diff_content[ 2: ] )
		result.diffs.append( diff )
		diff_content = diff_content[ diff.num_input_lines(): ]
	return result.normalize()

patches = Stack("patches")

def load( filename ):
	return _patch_from( filename, open( filename ).readlines() )

def push( patch ):
	if isinstance( patch, Patch ):
		return patches.push( patch )
	else:
		return patches.push( load( patch ) )

def pop():
	return patches.pop()

class TestError:

	def __init__( self, description, *args ):
		self.description = description % args

	def __repr__( self ): return "TestError( \"%s\" )" % self.description

def _check_empty( patch, message, *args ):
	if patch:
		patch = patch.shrinkwrapped()
	if patch:
		raise TestError( message % args )
	else:
		stdout.write('.')
		stdout.flush()

def main():
	argp = optparse.OptionParser()
	argp.add_option( "-t", "--test", action="store_true",    help="Perform consistency using on the given patches" )
	#argp.add_option( "patches", metavar="patch", nargs="*",  help="Names of patch files to load" ) # This is an argparse thing
	( args, patch_files ) = argp.parse_args()
	if len( patch_files ) >= 1:
		for patch in patch_files:
			push( patch )
		if args.test:
			# Testing that swapping adjacent patches has no overall effect
			print "Testing:"
			print "  Compatibility: ",
			for i in range( len(patches)-1 ):
				[ second, first ] = patches[ i:i+2 ]
				error_occurred = False
				try:
					first.compose( second )
				except PatlabError:
					error_occurred = True
				_check_empty( error_occurred, "Should be able to compose %s with %s", first.name, second.name )
			print " ok"
			print "  Checksum: ",
			checksum = patches.sum()
			for i in range( len(patches) ):
				checksum -= patches[i]
				_check_empty( None, "Pacifier" )
			_check_empty( error_occurred, "Sum minus all patches should be empty" )
			print " ok"
			print "  Swapping: ",
			for i in range( len(patches)-1 ):
				[ second, first ] = patches[ i:i+2 ]
				conflict = first % second
				if conflict:
					sanitized = first // second
					_check_empty( first - ( sanitized + conflict ), "without_conflicts plus conflict of %s with %s should equal %s", first.name, second.name, first.name )
					first = sanitized
				combo  = first + second
				over   = first >> second
				under  = second << first
				combo2 = under + over
				_check_empty( combo - combo2, "Swapping %s and %s made a difference", second.name, first.name )
				first2  = over << under
				second2 = under >> over
				_check_empty( first2 - first, "Under didn't undo over on %s", first.name )
				_check_empty( second2 - second, "Under didn't undo over on %s", second.name )
			print " ok"
			print "  Associativity: ",
			for i in range( len(patches)-2 ):
				[ third, second, first ] = patches[ i:i+3 ]
				combo  = first + ( second+third )
				combo2 = ( first+second ) + third
				_check_empty( combo - combo2, "Composition isn't associative on %s, %s, and %s", third.name, second.name, first.name )
			print " ok"

if 1:
	main()

if 0: # Debugging
	#push( "change.patch" )
	#push( "change-again.patch" )
	#push( "first.patch" )
	#push( "second.patch" )
	p1 = patches[1]
	p2 = patches[2]
	p3 = patches[3]
	#d2 = p2[2]
	#d3 = p3[19]
	#rem = p3 % p2
	#r1 = p2//p1
	#r2 = p1/p2
	#print p1+p2
	#print p1+p2
	#t = p1 - p1*p2
	#print t / p2

