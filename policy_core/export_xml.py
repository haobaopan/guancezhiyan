"""Shared OOXML escaping."""



def xml_escape(s):
    return (str(s).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
            .replace('"','&quot;').replace("'",'&apos;'))
