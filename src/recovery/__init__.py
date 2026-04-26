"""Startup reconciliation: pair orphaned DB/CLOB state before trading resumes.

FIX-05: without reconciliation, any crash between CLOB fill and the local
position insert (see FIX-03 for the write flow) leaves a 'pending' orders
row.  Those rows must be resolved before the bot generates new signals,
otherwise the operator can't tell fresh pending orders (legitimate) from
stale ones (orphaned).
"""
