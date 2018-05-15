#  This file is part of Lazylibrarian.
#
#  Lazylibrarian is free software':'you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Lazylibrarian is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Lazylibrarian.  If not, see <http://www.gnu.org/licenses/>.

import datetime
import os
import platform
import shutil
import tarfile
import threading
import traceback

import lazylibrarian
from lib.six import PY2

try:
    import zipfile
except ImportError:
    if PY2:
        import lib.zipfile as zipfile
    else:
        import lib3.zipfile as zipfile

from lazylibrarian import database, logger, utorrent, transmission, qbittorrent, \
    deluge, rtorrent, synology, sabnzbd, nzbget
from lazylibrarian.bookrename import seriesInfo, audioRename
from lazylibrarian.cache import cache_img
from lazylibrarian.calibre import calibredb
from lazylibrarian.common import scheduleJob, book_file, opf_file, setperm, bts_file, jpg_file, \
    safe_copy, safe_move, mymakedirs
from lazylibrarian.formatter import unaccented_str, unaccented, plural, now, today, is_valid_booktype, \
    replace_all, getList, surnameFirst, makeUnicode, makeBytestr, check_int
from lazylibrarian.gr import GoodReads
from lazylibrarian.importer import addAuthorToDB, addAuthorNameToDB, update_totals
from lazylibrarian.librarysync import get_book_info, find_book_in_db, LibraryScan
from lazylibrarian.magazinescan import create_id
from lazylibrarian.images import createMagCover
from lazylibrarian.notifiers import notify_download, custom_notify_download
from lib.deluge_client import DelugeRPCClient
from lib.fuzzywuzzy import fuzz

# Need to remove characters we don't want in the filename BEFORE adding to drive identifier
# as windows drive identifiers have colon, eg c:  but no colons allowed elsewhere in pathname
__dic__ = {'<': '', '>': '', '...': '', ' & ': ' ', ' = ': ' ', '?': '', '$': 's',
           ' + ': ' ', '"': '', ',': '', '*': '', ':': '', ';': '', '\'': '', '//': '/', '\\\\': '\\'}


def update_downloads(provider):
    myDB = database.DBConnection()
    entry = myDB.match('SELECT Count FROM downloads where Provider=?', (provider,))
    if entry:
        counter = int(entry['Count'])
        myDB.action('UPDATE downloads SET Count=? WHERE Provider=?', (counter + 1, provider))
    else:
        myDB.action('INSERT into downloads (Count, Provider) VALUES  (?, ?)', (1, provider))


def processAlternate(source_dir=None):
    # import a book from an alternate directory
    # noinspection PyBroadException
    try:
        if not source_dir or not os.path.isdir(source_dir):
            logger.warn("Alternate Directory not configured")
            return False
        if source_dir == lazylibrarian.DIRECTORY('eBook'):
            logger.warn('Alternate directory must not be the same as Destination')
            return False

        logger.debug('Processing alternate directory %s' % source_dir)
        # first, recursively process any books in subdirectories
        flist = os.listdir(makeBytestr(source_dir))
        flist = [makeUnicode(item) for item in flist]
        for fname in flist:
            subdir = os.path.join(source_dir, fname)
            if os.path.isdir(subdir):
                processAlternate(subdir)
        # only import one book from each alternate (sub)directory, this is because
        # the importer may delete the directory after importing a book,
        # depending on lazylibrarian.CONFIG['DESTINATION_COPY'] setting
        # also if multiple books in a folder and only a "metadata.opf"
        # which book is it for?
        new_book = book_file(source_dir, booktype='ebook')
        # Check for more than one book in the folder. Note we can't rely on basename
        # being the same, so just check for more than one bookfile of the same type
        filetypes = getList(lazylibrarian.CONFIG['EBOOK_TYPE'])
        reject = ''
        flist = os.listdir(makeBytestr(source_dir))
        flist = [makeUnicode(item) for item in flist]
        for item in filetypes:
            counter = 0
            for fname in flist:
                if fname.endswith(item):
                    counter += 1
                    if counter > 1:
                        reject = item
                        break
        if reject:
            logger.debug("Not processing %s, found multiple %s" % (source_dir, reject))
        elif new_book:
            metadata = {}
            # see if there is a metadata file in this folder with the info we need
            # try book_name.opf first, or fall back to any filename.opf
            metafile = os.path.splitext(new_book)[0] + '.opf'
            if not os.path.isfile(metafile):
                metafile = opf_file(source_dir)
            if metafile and os.path.isfile(metafile):
                try:
                    metadata = get_book_info(metafile)
                except Exception as e:
                    logger.warn('Failed to read metadata from %s, %s %s' % (metafile, type(e).__name__, str(e)))
            else:
                logger.debug('No metadata file found for %s' % new_book)
            if 'title' not in metadata or 'creator' not in metadata:
                # if not got both, try to get metadata from the book file
                extn = os.path.splitext(new_book)[1]
                if extn in [".epub", ".mobi"]:
                    if PY2:
                        new_book = new_book.encode(lazylibrarian.SYS_ENCODING)
                    try:
                        metadata = get_book_info(new_book)
                    except Exception as e:
                        logger.warn('No metadata found in %s, %s %s' % (new_book, type(e).__name__, str(e)))
            if 'title' in metadata and 'creator' in metadata:
                authorname = metadata['creator']
                bookname = metadata['title']
                myDB = database.DBConnection()
                authorid = ''
                authmatch = myDB.match('SELECT * FROM authors where AuthorName=?', (authorname,))

                if not authmatch:
                    # try goodreads preferred authorname
                    logger.debug("Checking GoodReads for [%s]" % authorname)
                    GR = GoodReads(authorname)
                    try:
                        author_gr = GR.find_author_id()
                    except Exception as e:
                        author_gr = {}
                        logger.warn("No author id for [%s] %s" % (authorname, type(e).__name__))
                    if author_gr:
                        grauthorname = author_gr['authorname']
                        authorid = author_gr['authorid']
                        logger.debug("GoodReads reports [%s] for [%s]" % (grauthorname, authorname))
                        authorname = grauthorname
                        authmatch = myDB.match('SELECT * FROM authors where AuthorID=?', (authorid,))

                if authmatch:
                    logger.debug("Author %s found in database" % authorname)
                else:
                    logger.debug("Author %s not found, adding to database" % authorname)
                    if authorid:
                        addAuthorToDB(authorid=authorid)
                    else:
                        addAuthorNameToDB(author=authorname)

                bookid = find_book_in_db(authorname, bookname, ignored=False)
                if bookid:
                    return import_book(source_dir, bookid)
                else:
                    bookid = find_book_in_db(authorname, bookname, ignored=True)
                    if bookid:
                        logger.warn("Book %s by %s is marked Ignored in database, importing anyway" %
                                    (bookname, authorname))
                        return import_book(source_dir, bookid)
                    logger.warn("Book %s by %s not found in database" % (bookname, authorname))
            else:
                logger.warn('Book %s has no metadata, unable to import' % new_book)
        else:
            # could check if an archive in this directory?
            logger.warn("No book file found in %s" % source_dir)
        return False
    except Exception:
        logger.error('Unhandled exception in processAlternate: %s' % traceback.format_exc())


def move_into_subdir(sourcedir, targetdir, fname, move='move'):
    # move the book and any related files too, other book formats, or opf, jpg with same title
    # (files begin with fname) from sourcedir to new targetdir
    # can't move metadata.opf or cover.jpg or similar as can't be sure they are ours
    # return how many files you moved
    cnt = 0
    list_dir = os.listdir(makeBytestr(sourcedir))
    list_dir = [makeUnicode(item) for item in list_dir]
    for ourfile in list_dir:
        if ourfile.startswith(fname) or is_valid_booktype(ourfile, booktype="audiobook"):
            if is_valid_booktype(ourfile, booktype="book") \
                    or is_valid_booktype(ourfile, booktype="audiobook") \
                    or is_valid_booktype(ourfile, booktype="mag") \
                    or os.path.splitext(ourfile)[1].lower() in ['.opf', '.jpg']:
                try:
                    srcfile = os.path.join(sourcedir, ourfile)
                    dstfile = os.path.join(targetdir, ourfile)
                    if lazylibrarian.CONFIG['DESTINATION_COPY'] or move == 'copy':
                        dstfile = safe_copy(srcfile, dstfile)
                        setperm(dstfile)
                        logger.debug("copy_into_subdir %s" % ourfile)
                        cnt += 1
                    else:
                        dstfile = safe_move(srcfile, dstfile)
                        setperm(dstfile)
                        logger.debug("move_into_subdir %s" % ourfile)
                        cnt += 1
                except Exception as why:
                    logger.warn("Failed to copy/move file %s to [%s], %s %s" %
                                (ourfile, targetdir, type(why).__name__, str(why)))
                    continue
    return cnt


def unpack_archive(pp_path, download_dir, title):
    """ See if pp_path is an archive containing a book
        returns new directory in download_dir with book in it, or empty string """
    # noinspection PyBroadException
    try:
        from lib.unrar import rarfile
        gotrar = True
    except Exception:
        gotrar = False
        rarfile = None

    targetdir = ''
    if not os.path.isfile(pp_path):  # regular files only
        targetdir = ''
    elif zipfile.is_zipfile(pp_path):
        if lazylibrarian.LOGLEVEL & lazylibrarian.log_postprocess:
            logger.debug('%s is a zip file' % pp_path)
        try:
            z = zipfile.ZipFile(pp_path)
        except Exception as e:
            logger.error("Failed to unzip %s: %s" % (pp_path, e))
            return ''

        namelist = z.namelist()
        for item in namelist:
            if is_valid_booktype(item, booktype="book") or is_valid_booktype(item, booktype="audiobook") \
                    or is_valid_booktype(item, booktype="mag"):
                if not targetdir:
                    targetdir = os.path.join(download_dir, title + '.unpack')
                if not os.path.isdir(targetdir):
                    res = mymakedirs(targetdir)
                    if not res:
                        return ''
                if PY2:
                    fmode = 'wb'
                else:
                    fmode = 'w'
                with open(os.path.join(targetdir, item), fmode) as f:
                    logger.debug('Extracting %s to %s' % (item, targetdir))
                    f.write(z.read(item))
            else:
                logger.debug('Skipping zipped file %s' % item)

    elif tarfile.is_tarfile(pp_path):
        if lazylibrarian.LOGLEVEL & lazylibrarian.log_postprocess:
            logger.debug('%s is a tar file' % pp_path)
        try:
            z = tarfile.TarFile(pp_path)
        except Exception as e:
            logger.error("Failed to untar %s: %s" % (pp_path, e))
            return ''

        namelist = z.getnames()
        for item in namelist:
            if is_valid_booktype(item, booktype="book") or is_valid_booktype(item, booktype="audiobook") \
                    or is_valid_booktype(item, booktype="mag"):
                if not targetdir:
                    targetdir = os.path.join(download_dir, title + '.unpack')
                if not os.path.isdir(targetdir):
                    res = mymakedirs(targetdir)
                    if not res:
                        return ''
                if PY2:
                    fmode = 'wb'
                else:
                    fmode = 'w'
                with open(os.path.join(targetdir, item), fmode) as f:
                    logger.debug('Extracting %s to %s' % (item, targetdir))
                    f.write(z.extractfile(item).read())
            else:
                logger.debug('Skipping tarred file %s' % item)

    elif gotrar and rarfile.is_rarfile(pp_path):
        if lazylibrarian.LOGLEVEL & lazylibrarian.log_postprocess:
            logger.debug('%s is a rar file' % pp_path)
        try:
            z = rarfile.RarFile(pp_path)
        except Exception as e:
            logger.error("Failed to unrar %s: %s" % (pp_path, e))
            return ''

        namelist = z.namelist()
        for item in namelist:
            if is_valid_booktype(item, booktype="book") or is_valid_booktype(item, booktype="audiobook") \
                    or is_valid_booktype(item, booktype="mag"):
                if not targetdir:
                    targetdir = os.path.join(download_dir, title + '.unpack')
                if not os.path.isdir(targetdir):
                    res = mymakedirs(targetdir)
                    if not res:
                        return ''
                if PY2:
                    fmode = 'wb'
                else:
                    fmode = 'w'
                with open(os.path.join(targetdir, item), fmode) as f:
                    logger.debug('Extracting %s to %s' % (item, targetdir))
                    f.write(z.read(item))
            else:
                logger.debug('Skipping rarred file %s' % item)
    else:
        logger.debug('[%s] Not a recognised archive' % pp_path)
    return targetdir


def cron_processDir():
    if 'POSTPROCESS' not in [n.name for n in [t for t in threading.enumerate()]]:
        processDir()


def bookType(book):
    book_type = book['AuxInfo']
    if book_type != 'AudioBook' and book_type != 'eBook':
        if book_type is None or book_type == '':
            book_type = 'eBook'
        else:
            book_type = 'Magazine'
    return book_type


def processDir(reset=False, startdir=None, ignoreclient=False):
    count = 0
    for threadname in [n.name for n in [t for t in threading.enumerate()]]:
        if threadname == 'POSTPROCESS':
            count += 1

    threadname = threading.currentThread().name
    if threadname == 'POSTPROCESS':
        count -= 1
    if count:
        logger.debug("POSTPROCESS is already running")
        return

    threading.currentThread().name = "POSTPROCESS"
    # noinspection PyBroadException,PyStatementEffect
    try:
        ppcount = 0
        myDB = database.DBConnection()
        skipped_extensions = getList(lazylibrarian.CONFIG['SKIPPED_EXT'])
        banned_extensions = getList(lazylibrarian.CONFIG['BANNED_EXT'])
        if startdir:
            templist = [startdir]
        else:
            templist = getList(lazylibrarian.CONFIG['DOWNLOAD_DIR'], ',')
            if len(templist) and lazylibrarian.DIRECTORY("Download") != templist[0]:
                templist.insert(0, lazylibrarian.DIRECTORY("Download"))
        dirlist = []
        for item in templist:
            if os.path.isdir(item):
                dirlist.append(item)
            else:
                logger.debug("[%s] is not a directory" % item)

        snatched = myDB.select('SELECT * from wanted WHERE Status="Snatched"')
        logger.debug('Found %s file%s marked "Snatched"' % (len(snatched), plural(len(snatched))))
        if len(snatched):
            for book in snatched:
                # if torrent, see if we can get current status from the downloader as the name
                # may have been changed once magnet resolved, or download started or completed
                # depending on torrent downloader. Usenet doesn't change the name. We like usenet.
                matchtitle = unaccented_str(book['NZBtitle'])
                torrentname = getTorrentName(matchtitle, book['Source'], book['DownloadID'])

                if torrentname and torrentname != matchtitle:
                    logger.debug("%s Changing [%s] to [%s]" % (book['Source'], matchtitle, torrentname))
                    # should we check against reject word list again as the name has changed?
                    myDB.action('UPDATE wanted SET NZBtitle=? WHERE NZBurl=?', (torrentname, book['NZBurl']))
                    matchtitle = torrentname

                book_type = bookType(book)
                if book_type == 'eBook':
                    maxsize = lazylibrarian.CONFIG['REJECT_MAXSIZE']
                    minsize = lazylibrarian.CONFIG['REJECT_MINSIZE']
                    filetypes = lazylibrarian.CONFIG['EBOOK_TYPE']
                elif book_type == 'AudioBook':
                    maxsize = lazylibrarian.CONFIG['REJECT_MAXAUDIO']
                    # minsize = lazylibrarian.CONFIG['REJECT_MINAUDIO']
                    minsize = 0  # individual audiobook chapters can be quite small
                    filetypes = lazylibrarian.CONFIG['AUDIOBOOK_TYPE']
                elif book_type == 'Magazine':
                    maxsize = lazylibrarian.CONFIG['REJECT_MAGSIZE']
                    minsize = lazylibrarian.CONFIG['REJECT_MAGMIN']
                    filetypes = lazylibrarian.CONFIG['MAG_TYPE']
                else:  # shouldn't happen
                    maxsize = 0
                    minsize = 0
                    filetypes = ''

                # here we could also check percentage downloaded or eta or status?
                # If downloader says it hasn't completed, no need to look for it.
                rejected = False
                if book['Source'] in ['TRANSMISSION', 'QBITTORRENT', 'DELUGEWEBUI', 'DELUGERPC']:
                    torrentfiles = getTorrentFiles(book['Source'], book['DownloadID'])
                    # Downloaders return varying amounts of info using varying names
                    if not torrentfiles:  # empty
                        logger.debug("No files returned by %s for %s" % (book['Source'], matchtitle))
                    else:
                        logger.debug("Checking files in %s" % matchtitle)
                        for entry in torrentfiles:
                            fname = ''
                            fsize = 0
                            if 'path' in entry:  # deluge
                                fname = entry['path']
                            if 'size' in entry:  # deluge, qbittorrent
                                fsize = entry['size']
                            if 'length' in entry:  # transmission
                                fsize = entry['length']
                            if 'name' in entry:  # transmission, qbittorrent
                                fname = entry['name']
                            extn = os.path.splitext(fname)[1].lstrip('.').lower()
                            if extn and extn in banned_extensions:
                                logger.warn("%s contains %s. Deleting torrent" % (matchtitle, extn))
                                rejected = True
                                break
                            # only check size on right types of file
                            # eg dont reject cos jpg is smaller than min file size
                            # need to check if we have a size in K M or just a number. If K or M could be a float.
                            if fsize and extn in filetypes:
                                try:
                                    if 'M' in str(fsize):
                                        fsize = int(float(fsize.split('M')[0].strip()) * 1048576)
                                    elif 'K' in str(fsize):
                                        fsize = int(float(fsize.split('K')[0].strip() * 1024))
                                    fsize = round(check_int(fsize, 0) / 1048576.0, 2)  # float to 2dp in Mb
                                except ValueError:
                                    fsize = 0
                                if fsize:
                                    if maxsize and fsize > maxsize:
                                        logger.warn("%s is too large (%sMb). Deleting torrent" % (fname, fsize))
                                        rejected = True
                                        break
                                    if minsize and fsize < minsize:
                                        logger.warn("%s is too small (%sMb). Deleting torrent" % (fname, fsize))
                                        rejected = True
                                        break
                            if not rejected:
                                logger.debug("%s: (%sMb) is wanted" % (fname, fsize))
                if rejected:
                    # change status to "Failed", and ask downloader to delete task and files
                    # Only reset book status to wanted if still snatched in case another download task succeeded
                    if book['BookID'] != 'unknown':
                        cmd = ''
                        if book_type == 'eBook':
                            cmd = 'UPDATE books SET status="Wanted" WHERE status="Snatched" and BookID=?'
                        elif book_type == 'AudioBook':
                            cmd = 'UPDATE books SET audiostatus="Wanted" WHERE audiostatus="Snatched" and BookID=?'
                        if cmd:
                            myDB.action(cmd, (book['BookID'],))
                        myDB.action('UPDATE wanted SET Status="Failed" WHERE BookID=?', (book['BookID'],))
                        delete_task(book['Source'], book['DownloadID'], True)

        for download_dir in dirlist:
            try:
                downloads = os.listdir(makeBytestr(download_dir))
                downloads = [makeUnicode(item) for item in downloads]
            except OSError as why:
                logger.error('Could not access directory [%s] %s' % (download_dir, why.strerror))
                threading.currentThread().name = "WEBSERVER"
                return

            logger.debug('Found %s file%s in %s' % (len(downloads), plural(len(downloads)), download_dir))

            # any books left to look for...
            snatched = myDB.select('SELECT * from wanted WHERE Status="Snatched"')
            if len(snatched):
                for book in snatched:
                    book_type = bookType(book)
                    matchtitle = unaccented_str(book['NZBtitle'])
                    matches = []
                    logger.debug('Looking for %s %s in %s' % (book_type, matchtitle, download_dir))

                    for fname in downloads:
                        # skip if failed before or incomplete torrents, or incomplete btsync etc
                        if lazylibrarian.LOGLEVEL & lazylibrarian.log_postprocess:
                            logger.debug("Checking extn on %s" % fname)
                        extn = os.path.splitext(fname)[1]
                        if not extn or extn.strip('.') not in skipped_extensions:
                            # This is to get round differences in torrent filenames.
                            # Usenet is ok, but Torrents aren't always returned with the name we searched for
                            # We ask the torrent downloader for the torrent name, but don't always get an answer
                            # so we try to do a "best match" on the name, there might be a better way...

                            matchname = fname
                            # torrents might have words_separated_by_underscores
                            matchname = matchname.split(' LL.(')[0].replace('_', ' ')
                            matchtitle = matchtitle.split(' LL.(')[0].replace('_', ' ')
                            match = fuzz.token_set_ratio(matchtitle, matchname)
                            if lazylibrarian.LOGLEVEL & lazylibrarian.log_fuzz:
                                logger.debug("%s%% match %s : %s" % (match, matchtitle, matchname))
                            if match >= lazylibrarian.CONFIG['DLOAD_RATIO']:
                                pp_path = os.path.join(download_dir, fname)

                                if lazylibrarian.LOGLEVEL & lazylibrarian.log_postprocess:
                                    logger.debug("processDir found %s %s" % (type(pp_path), repr(pp_path)))

                                if os.path.isfile(pp_path):
                                    # Check for single file downloads first. Book/mag file in download root.
                                    # move the file into it's own subdirectory so we don't move/delete
                                    # things that aren't ours
                                    # note that epub are zipfiles so check booktype first
                                    #
                                    if is_valid_booktype(fname, booktype="book") \
                                            or is_valid_booktype(fname, booktype="audiobook") \
                                            or is_valid_booktype(fname, booktype="mag"):
                                        if lazylibrarian.LOGLEVEL & lazylibrarian.log_postprocess:
                                            logger.debug('file [%s] is a valid book/mag' % fname)
                                        if bts_file(download_dir):
                                            logger.debug("Skipping %s, found a .bts file" % download_dir)
                                        else:
                                            aname = os.path.splitext(fname)[0]
                                            while aname[-1] in '_. ':
                                                aname = aname[:-1]

                                            if lazylibrarian.CONFIG['DESTINATION_COPY'] or \
                                                    (book['NZBmode'] in ['torrent', 'magnet', 'torznab'] and
                                                     lazylibrarian.CONFIG['KEEP_SEEDING']):
                                                targetdir = os.path.join(download_dir, aname + '.unpack')
                                                move = 'copy'
                                            else:
                                                targetdir = os.path.join(download_dir, aname)
                                                move = 'move'

                                            if not os.path.isdir(targetdir):
                                                _ = mymakedirs(targetdir)

                                            if os.path.isdir(targetdir):
                                                cnt = move_into_subdir(download_dir, targetdir, aname, move=move)
                                                if cnt:
                                                    pp_path = targetdir
                                                else:
                                                    try:
                                                        os.rmdir(targetdir)
                                                    except OSError as why:
                                                        logger.warn("Unable to delete %s: %s" %
                                                                    (targetdir, why.strerror))
                                    else:
                                        # Is file an archive, if so look inside and extract to new dir
                                        res = unpack_archive(pp_path, download_dir, matchtitle)
                                        if res:
                                            pp_path = res
                                        else:
                                            logger.debug('Skipping unhandled file %s' % fname)

                                if os.path.isdir(pp_path):
                                    logger.debug('Found folder (%s%%) [%s] for %s %s' %
                                                 (match, pp_path, book_type, matchtitle))

                                    for f in os.listdir(makeBytestr(pp_path)):
                                        f = makeUnicode(f)
                                        if not is_valid_booktype(f, 'book') \
                                                and not is_valid_booktype(f, 'audiobook') \
                                                and not is_valid_booktype(f, 'mag'):
                                            # Is file an archive, if so look inside and extract to new dir
                                            res = unpack_archive(os.path.join(pp_path, f), pp_path, matchtitle)
                                            if res:
                                                pp_path = res
                                                break

                                    skipped = False
                                    if book_type == 'eBook' and not book_file(pp_path, 'ebook'):
                                        logger.debug("Skipping %s, no ebook found" % pp_path)
                                        skipped = True
                                    elif book_type == 'AudioBook' and not book_file(pp_path, 'audiobook'):
                                        logger.debug("Skipping %s, no audiobook found" % pp_path)
                                        skipped = True
                                    elif book_type == 'Magazine' and not book_file(pp_path, 'mag'):
                                        logger.debug("Skipping %s, no magazine found" % pp_path)
                                        skipped = True
                                    if not os.listdir(makeBytestr(pp_path)):
                                        logger.debug("Skipping %s, folder is empty" % pp_path)
                                        skipped = True
                                    elif bts_file(pp_path):
                                        logger.debug("Skipping %s, found a .bts file" % pp_path)
                                        skipped = True
                                    if not skipped:
                                        matches.append([match, pp_path, book])
                                        if match == 100:  # no point looking any further
                                            break
                                else:
                                    logger.debug('%s is not a file or a directory?' % pp_path)
                            else:
                                pp_path = os.path.join(download_dir, fname)
                                matches.append([match, pp_path, book])  # so we can report closest match
                        else:
                            logger.debug('Skipping %s' % fname)

                    match = 0
                    pp_path = ''
                    dest_path = ''
                    authorname = ''
                    bookname = ''
                    global_name = ''
                    mostrecentissue = ''
                    if matches:
                        highest = max(matches, key=lambda x: x[0])
                        match = highest[0]
                        pp_path = highest[1]
                        book = highest[2]  # type: dict
                    if match and match >= lazylibrarian.CONFIG['DLOAD_RATIO']:
                        logger.debug('Found match (%s%%): %s for %s %s' % (
                                     match, pp_path, book_type, book['NZBtitle']))

                        cmd = 'SELECT AuthorName,BookName from books,authors WHERE BookID=?'
                        cmd += ' and books.AuthorID = authors.AuthorID'
                        data = myDB.match(cmd, (book['BookID'],))
                        if data:  # it's ebook/audiobook
                            logger.debug('Processing %s %s' % (book_type, book['BookID']))
                            authorname = data['AuthorName']
                            authorname = ' '.join(authorname.split())  # ensure no extra whitespace
                            bookname = data['BookName']
                            if 'windows' in platform.system().lower() and '/' in \
                                    lazylibrarian.CONFIG['EBOOK_DEST_FOLDER']:
                                logger.warn('Please check your EBOOK_DEST_FOLDER setting')
                                lazylibrarian.CONFIG['EBOOK_DEST_FOLDER'] = lazylibrarian.CONFIG[
                                    'EBOOK_DEST_FOLDER'].replace('/', '\\')
                            # Default destination path, should be allowed change per config file.
                            seriesinfo = seriesInfo(book['BookID'])
                            dest_path = lazylibrarian.CONFIG['EBOOK_DEST_FOLDER'].replace(
                                '$Author', authorname).replace(
                                '$Title', bookname).replace(
                                '$Series', seriesinfo['Full']).replace(
                                '$SerName', seriesinfo['Name']).replace(
                                '$SerNum', seriesinfo['Num']).replace(
                                '$$', ' ')
                            dest_path = ' '.join(dest_path.split()).strip()
                            dest_path = replace_all(dest_path, __dic__)
                            dest_dir = lazylibrarian.DIRECTORY('eBook')
                            if book_type == 'AudioBook' and lazylibrarian.DIRECTORY('Audio'):
                                dest_dir = lazylibrarian.DIRECTORY('Audio')
                            dest_path = os.path.join(dest_dir, dest_path)

                            global_name = lazylibrarian.CONFIG['EBOOK_DEST_FILE'].replace(
                                '$Author', authorname).replace(
                                '$Title', bookname).replace(
                                '$Series', '').replace(
                                '$SerName', '').replace(
                                '$SerNum', '').replace(
                                '$$', ' ')
                            global_name = ' '.join(global_name.split()).strip()
                        else:
                            data = myDB.match('SELECT IssueDate from magazines WHERE Title=?', (book['BookID'],))
                            if data:  # it's a magazine
                                logger.debug('Processing magazine %s' % book['BookID'])
                                # AuxInfo was added for magazine release date, normally housed in 'magazines'
                                # but if multiple files are downloading, there will be an error in post-processing
                                # trying to go to the same directory.
                                mostrecentissue = data['IssueDate']  # keep for processing issues arriving out of order
                                mag_name = unaccented_str(replace_all(book['BookID'], __dic__))
                                # book auxinfo is a cleaned date, eg 2015-01-01
                                dest_path = lazylibrarian.CONFIG['MAG_DEST_FOLDER'].replace(
                                    '$IssueDate', book['AuxInfo']).replace('$Title', mag_name)

                                if lazylibrarian.CONFIG['MAG_RELATIVE']:
                                    dest_dir = lazylibrarian.DIRECTORY('eBook')
                                    dest_path = os.path.join(dest_dir, dest_path)
                                else:
                                    ignorefile = os.path.join(dest_path, '.ll_ignore')
                                    with open(ignorefile, 'a'):
                                        os.utime(ignorefile, None)
                                if PY2:
                                    dest_path = dest_path.encode(lazylibrarian.SYS_ENCODING)
                                global_name = lazylibrarian.CONFIG['MAG_DEST_FILE'].replace(
                                    '$IssueDate', book['AuxInfo']).replace('$Title', mag_name)
                                global_name = unaccented(global_name)
                            else:  # not recognised, maybe deleted
                                logger.debug('Nothing in database matching "%s"' % book['BookID'])
                                controlValueDict = {"BookID": book['BookID'], "Status": "Snatched"}
                                newValueDict = {"Status": "Failed", "NZBDate": now()}
                                myDB.upsert("wanted", newValueDict, controlValueDict)
                    else:
                        logger.debug("Snatched %s %s is not in download directory" %
                                     (book['NZBmode'], book['NZBtitle']))
                        if match:
                            logger.debug('Closest match (%s%%): %s' % (match, pp_path))
                            if lazylibrarian.LOGLEVEL & lazylibrarian.log_fuzz:
                                for match in matches:
                                    logger.debug('Match: %s%%  %s' % (match[0], match[1]))

                    if not dest_path:
                        continue

                    success, dest_file = processDestination(pp_path, dest_path, authorname, bookname,
                                                            global_name, book['BookID'], book_type)
                    if success:
                        logger.debug("Processed %s: %s, %s" % (book['NZBmode'], global_name, book['NZBurl']))
                        # only update the snatched ones in case some already marked failed/processed in history
                        controlValueDict = {"NZBurl": book['NZBurl'], "Status": "Snatched"}
                        newValueDict = {"Status": "Processed", "NZBDate": now()}  # say when we processed it
                        myDB.upsert("wanted", newValueDict, controlValueDict)

                        if bookname:  # it's ebook or audiobook
                            processExtras(dest_file, global_name, book['BookID'], book_type)
                            iss_id = 0
                        else:  # update mags
                            if mostrecentissue:
                                if mostrecentissue.isdigit() and str(book['AuxInfo']).isdigit():
                                    older = (int(mostrecentissue) > int(book['AuxInfo']))  # issuenumber
                                else:
                                    older = (mostrecentissue > book['AuxInfo'])  # YYYY-MM-DD
                            else:
                                older = False

                            controlValueDict = {"Title": book['BookID']}
                            if older:  # check this in case processing issues arriving out of order
                                newValueDict = {"LastAcquired": today(), "IssueStatus": "Open"}
                            else:
                                newValueDict = {"IssueDate": book['AuxInfo'], "LastAcquired": today(),
                                                "LatestCover": os.path.splitext(dest_file)[0] + '.jpg',
                                                "IssueStatus": "Open"}
                            myDB.upsert("magazines", newValueDict, controlValueDict)

                            iss_id = create_id("%s %s" % (book['BookID'], book['AuxInfo']))
                            controlValueDict = {"Title": book['BookID'], "IssueDate": book['AuxInfo']}
                            newValueDict = {"IssueAcquired": today(),
                                            "IssueFile": dest_file,
                                            "IssueID": iss_id
                                            }
                            myDB.upsert("issues", newValueDict, controlValueDict)

                            # create a thumbnail cover for the new issue
                            createMagCover(dest_file)
                            processMAGOPF(dest_file, book['BookID'], book['AuxInfo'], iss_id)
                            if lazylibrarian.CONFIG['IMP_AUTOADDMAG']:
                                dest_path = os.path.dirname(dest_file)
                                processAutoAdd(dest_path, booktype='mag')

                        # calibre or ll copied/moved the files we want, now delete source files

                        to_delete = True
                        if ignoreclient is False and book['NZBmode'] in ['torrent', 'magnet', 'torznab']:
                            # Only delete torrents if we don't want to keep seeding
                            if lazylibrarian.CONFIG['KEEP_SEEDING']:
                                logger.warn('%s is seeding %s %s' % (book['Source'], book['NZBmode'], book['NZBtitle']))
                                to_delete = False

                        if ignoreclient is False and to_delete:
                            # ask downloader to delete the torrent, but not the files
                            # we may delete them later, depending on other settings
                            if not book['Source']:
                                logger.warn("Unable to remove %s, no source" % book['NZBtitle'])
                            elif not book['DownloadID'] or book['DownloadID'] == "unknown":
                                logger.warn("Unable to remove %s from %s, no DownloadID" %
                                            (book['NZBtitle'], book['Source'].lower()))
                            elif book['Source'] != 'DIRECT':
                                logger.debug('Removing %s from %s' % (book['NZBtitle'], book['Source'].lower()))
                                delete_task(book['Source'], book['DownloadID'], False)

                        if to_delete:
                            # only delete the files if not in download root dir and DESTINATION_COPY not set
                            # always delete files we unpacked from an archive
                            if lazylibrarian.CONFIG['DESTINATION_COPY']:
                                to_delete = False
                            if pp_path == download_dir:
                                to_delete = False
                            if pp_path.endswith('.unpack'):
                                to_delete = True
                            if to_delete:
                                if os.path.isdir(pp_path):
                                    # calibre might have already deleted it?
                                    try:
                                        shutil.rmtree(pp_path)
                                        logger.debug('Deleted %s, %s from %s' %
                                                     (book['NZBtitle'], book['NZBmode'], book['Source'].lower()))
                                    except Exception as why:
                                        logger.warn("Unable to remove %s, %s %s" %
                                                    (pp_path, type(why).__name__, str(why)))
                            else:
                                if lazylibrarian.CONFIG['DESTINATION_COPY']:
                                    logger.debug("Not removing original files as Keep Files is set")
                                else:
                                    logger.debug("Not removing original files as in download root")

                        logger.info('Successfully processed: %s' % global_name)

                        ppcount += 1
                        if bookname:
                            custom_notify_download("%s %s" % (book['BookID'], book_type))
                            notify_download("%s %s from %s at %s" %
                                            (book_type, global_name, book['NZBprov'], now()), book['BookID'])
                        else:
                            custom_notify_download("%s %s" % (book['BookID'], book['NZBUrl']))
                            notify_download("%s %s from %s at %s" %
                                            (book_type, global_name, book['NZBprov'], now()), iss_id)

                        update_downloads(book['NZBprov'])
                    else:
                        logger.error('Postprocessing for %s has failed: %s' % (global_name, dest_file))
                        controlValueDict = {"NZBurl": book['NZBurl'], "Status": "Snatched"}
                        newValueDict = {"Status": "Failed", "NZBDate": now()}
                        myDB.upsert("wanted", newValueDict, controlValueDict)
                        # if it's a book, reset status so we try for a different version
                        # if it's a magazine, user can select a different one from pastissues table
                        if book_type == 'eBook':
                            myDB.action('UPDATE books SET status="Wanted" WHERE BookID=?', (book['BookID'],))
                        elif book_type == 'AudioBook':
                            myDB.action('UPDATE books SET audiostatus="Wanted" WHERE BookID=?', (book['BookID'],))

                        # at this point, as it failed we should move it or it will get postprocessed
                        # again (and fail again)
                        if os.path.isdir(pp_path + '.fail'):
                            try:
                                shutil.rmtree(pp_path + '.fail')
                            except Exception as why:
                                logger.warn("Unable to remove %s, %s %s" %
                                            (pp_path + '.fail', type(why).__name__, str(why)))
                        try:
                            _ = safe_move(pp_path, pp_path + '.fail')
                            logger.warn('Residual files remain in %s.fail' % pp_path)
                        except Exception as why:
                            logger.error("Unable to rename %s, %s %s" %
                                         (pp_path, type(why).__name__, str(why)))
                            logger.warn('Residual files remain in %s' % pp_path)

            if downloads:
                ppcount += check_residual(download_dir)

        logger.info('%s book%s/mag%s processed.' % (ppcount, plural(ppcount), plural(ppcount)))

        # Now check for any that are still marked snatched...
        snatched = myDB.select('SELECT * from wanted WHERE Status="Snatched"')
        if lazylibrarian.CONFIG['TASK_AGE'] and len(snatched):
            for book in snatched:
                book_type = bookType(book)
                # FUTURE: we could check percentage downloaded or eta?
                # if percentage is increasing, it's just slow
                try:
                    when_snatched = datetime.datetime.strptime(book['NZBdate'], '%Y-%m-%d %H:%M:%S')
                    timenow = datetime.datetime.now()
                    td = when_snatched - timenow
                    diff = td.seconds  # time difference in seconds
                except ValueError:
                    diff = 0
                hours = int(diff / 3600)
                if hours >= lazylibrarian.CONFIG['TASK_AGE']:
                    if book['Source'] and book['Source'] != 'DIRECT':
                        logger.warn('%s was sent to %s %s hours ago, deleting failed task' %
                                    (book['NZBtitle'], book['Source'].lower(), hours))
                    # change status to "Failed", and ask downloader to delete task and files
                    # Only reset book status to wanted if still snatched in case another download task succeeded
                    if book['BookID'] != 'unknown':
                        cmd = ''
                        if book_type == 'eBook':
                            cmd = 'UPDATE books SET status="Wanted" WHERE status="Snatched" and BookID=?'
                        elif book_type == 'AudioBook':
                            cmd = 'UPDATE books SET audiostatus="Wanted" WHERE audiostatus="Snatched" and BookID=?'
                        if cmd:
                            myDB.action(cmd, (book['BookID'],))
                        myDB.action('UPDATE wanted SET Status="Failed" WHERE BookID=?', (book['BookID'],))
                        delete_task(book['Source'], book['DownloadID'], True)

        # Check if postprocessor needs to run again
        snatched = myDB.select('SELECT * from wanted WHERE Status="Snatched"')
        if len(snatched) == 0:
            logger.info('Nothing marked as snatched. Stopping postprocessor.')
            scheduleJob(action='Stop', target='processDir')

        if reset:
            scheduleJob(action='Restart', target='processDir')

    except Exception:
        logger.error('Unhandled exception in processDir: %s' % traceback.format_exc())

    finally:
        threading.currentThread().name = threadname


def check_residual(download_dir):
    # Import any books in download that weren't marked as snatched, but have a LL.(bookid)
    # don't process any we've already got as we might not want to delete originals
    myDB = database.DBConnection()
    skipped_extensions = getList(lazylibrarian.CONFIG['SKIPPED_EXT'])
    ppcount = 0
    downloads = os.listdir(makeBytestr(download_dir))
    downloads = [makeUnicode(item) for item in downloads]
    if lazylibrarian.LOGLEVEL & lazylibrarian.log_postprocess:
        logger.debug("Scanning %s entries in %s for LL.(num)" % (len(downloads), download_dir))
    for entry in downloads:
        if "LL.(" in entry:
            _, extn = os.path.splitext(entry)
            if not extn or extn.strip('.') not in skipped_extensions:
                bookID = entry.split("LL.(")[1].split(")")[0]
                logger.debug("Book with id: %s found in download directory" % bookID)
                data = myDB.match('SELECT BookFile from books WHERE BookID=?', (bookID,))
                if data and data['BookFile'] and os.path.isfile(data['BookFile']):
                    logger.debug('Skipping BookID %s, already exists' % bookID)
                else:
                    pp_path = os.path.join(download_dir, entry)

                    if lazylibrarian.LOGLEVEL & lazylibrarian.log_postprocess:
                        logger.debug("Checking type of %s" % pp_path)

                    if os.path.isfile(pp_path):
                        if lazylibrarian.LOGLEVEL & lazylibrarian.log_postprocess:
                            logger.debug("%s is a file" % pp_path)
                        pp_path = os.path.join(download_dir)

                    if os.path.isdir(pp_path):
                        if lazylibrarian.LOGLEVEL & lazylibrarian.log_postprocess:
                            logger.debug("%s is a dir" % pp_path)
                        if import_book(pp_path, bookID):
                            if lazylibrarian.LOGLEVEL & lazylibrarian.log_postprocess:
                                logger.debug("Imported %s" % pp_path)
                            ppcount += 1
            else:
                if lazylibrarian.LOGLEVEL & lazylibrarian.log_postprocess:
                    logger.debug("Skipping extn %s" % entry)
        else:
            if lazylibrarian.LOGLEVEL & lazylibrarian.log_postprocess:
                logger.debug("Skipping (not LL) %s" % entry)
    return ppcount


def getTorrentName(title, source, downloadid):
    torrentname = None
    try:
        logger.debug("getTorrentName: %s was sent to %s" % (title, source))
        if source == 'TRANSMISSION':
            torrentname = transmission.getTorrentFolder(downloadid)
        elif source == 'QBITTORRENT':
            torrentname = qbittorrent.getName(downloadid)
        # elif source == 'UTORRENT':
        #    torrentname = utorrent.nameTorrent(downloadid)
        # elif source == 'RTORRENT':
        #    torrentname = rtorrent.getName(downloadid)
        # elif source == 'SYNOLOGY_TOR':
        #    torrentname = synology.getName(downloadid)
        elif source == 'DELUGEWEBUI':
            torrentname = deluge.getTorrentFolder(downloadid)
        elif source == 'DELUGERPC':
            client = DelugeRPCClient(lazylibrarian.CONFIG['DELUGE_HOST'], int(lazylibrarian.CONFIG['DELUGE_PORT']),
                                     lazylibrarian.CONFIG['DELUGE_USER'], lazylibrarian.CONFIG['DELUGE_PASS'])
            try:
                client.connect()
                result = client.call('core.get_torrent_status', downloadid, {})
                # for item in result:
                #     logger.debug ('Deluge RPC result %s: %s' % (item, result[item]))
                if 'name' in result:
                    torrentname = unaccented_str(result['name'])
            except Exception as e:
                logger.error('DelugeRPC failed %s %s' % (type(e).__name__, str(e)))
        return torrentname

    except Exception as e:
        logger.error("Failed to get updated torrent name from %s for %s: %s %s" %
                     (source, downloadid, type(e).__name__, str(e)))
        return None


def getTorrentFiles(source, downloadid):
    torrentfiles = None
    try:
        if source == 'TRANSMISSION':
            torrentfiles = transmission.getTorrentFiles(downloadid)
        # elif source == 'UTORRENT':
        #     torrentname = utorrent.nameTorrent(downloadid)
        # elif source == 'RTORRENT':
        #     torrentname = rtorrent.getName(downloadid)
        elif source == 'QBITTORRENT':
            torrentfiles = qbittorrent.getFiles(downloadid)
        # elif source == 'SYNOLOGY_TOR':
        #     torrentname = synology.getName(downloadid)
        elif source == 'DELUGEWEBUI':
            torrentfiles = deluge.getTorrentFiles(downloadid)
        elif source == 'DELUGERPC':
            client = DelugeRPCClient(lazylibrarian.CONFIG['DELUGE_HOST'], int(lazylibrarian.CONFIG['DELUGE_PORT']),
                                     lazylibrarian.CONFIG['DELUGE_USER'], lazylibrarian.CONFIG['DELUGE_PASS'])
            try:
                client.connect()
                result = client.call('core.get_torrent_status', downloadid, {})
                if 'files' in result:
                    torrentfiles = result['files']
            except Exception as e:
                logger.error('DelugeRPC failed %s %s' % (type(e).__name__, str(e)))
        return torrentfiles

    except Exception as e:
        logger.error("Failed to get torrent files from %s for %s: %s %s" %
                     (source, downloadid, type(e).__name__, str(e)))
        return None


def getDownloadProgress(source, downloadid):
    progress = 0
    try:
        if source == 'TRANSMISSION':
            progress = transmission.getTorrentProgress(downloadid)
        elif source == 'SABNZBD':
            res = sabnzbd.SABnzbd(nzburl='queue')
            found = False
            if res and 'queue' in res:
                for item in res['queue']['slots']:
                    if item['nzo_id'] == downloadid:
                        found = True
                        progress = item['percentage']
                        break
            if not found:  # not in queue, try history in case already completed
                res = sabnzbd.SABnzbd(nzburl='history')
                if res and 'history' in res:
                    for item in res['history']['slots']:
                        if item['nzo_id'] == downloadid:
                            # 100% if completed, 99% if still extracting, 0% if not found
                            if item['status'] == 'Completed':
                                progress = 100
                            elif item['status'] == 'Extracting':
                                progress = 99
                            elif item['status'] == 'Failed':
                                progress = -1
                            break
        elif source == 'NZBGET':
            res = nzbget.sendNZB(cmd='listgroups', nzbID=downloadid)
            for item in res:
                if item['NZBID'] == downloadid:
                    total = item['FileSizeHi'] << 32 + item['FileSizeLo']
                    remaining = item['RemainingSizeHi'] << 32 + item['RemainingSizeLo']
                    done = total - remaining
                    progress = done * 100 / total
                    break
        # elif source == 'UTORRENT':
        #     torrentname = utorrent.nameTorrent(downloadid)
        # elif source == 'RTORRENT':
        #     torrentname = rtorrent.getName(downloadid)
        elif source == 'QBITTORRENT':
            progress = qbittorrent.getProgress(downloadid)
        # elif source == 'SYNOLOGY_TOR':
        #     torrentname = synology.getName(downloadid)
        elif source == 'DELUGEWEBUI':
            progress = deluge.getTorrentProgress(downloadid)
        elif source == 'DELUGERPC':
            client = DelugeRPCClient(lazylibrarian.CONFIG['DELUGE_HOST'], int(lazylibrarian.CONFIG['DELUGE_PORT']),
                                     lazylibrarian.CONFIG['DELUGE_USER'], lazylibrarian.CONFIG['DELUGE_PASS'])
            try:
                client.connect()
                result = client.call('core.get_torrent_status', downloadid, {})
                if 'percentDone' in result:
                    progress = result['percentDone']
            except Exception as e:
                logger.error('DelugeRPC failed %s %s' % (type(e).__name__, str(e)))

        return progress

    except Exception as e:
        logger.error("Failed to get torrent progress from %s for %s: %s %s" %
                     (source, downloadid, type(e).__name__, str(e)))
        return 0


def delete_task(Source, DownloadID, remove_data):
    try:
        if Source == "BLACKHOLE":
            logger.warn("Download %s has not been processed from blackhole" % DownloadID)
        elif Source == "SABNZBD":
            sabnzbd.SABnzbd(DownloadID, 'delete', remove_data)
            sabnzbd.SABnzbd(DownloadID, 'delhistory', remove_data)
        elif Source == "NZBGET":
            nzbget.deleteNZB(DownloadID, remove_data)
        elif Source == "UTORRENT":
            utorrent.removeTorrent(DownloadID, remove_data)
        elif Source == "RTORRENT":
            rtorrent.removeTorrent(DownloadID, remove_data)
        elif Source == "QBITTORRENT":
            qbittorrent.removeTorrent(DownloadID, remove_data)
        elif Source == "TRANSMISSION":
            transmission.removeTorrent(DownloadID, remove_data)
        elif Source == "SYNOLOGY_TOR" or Source == "SYNOLOGY_NZB":
            synology.removeTorrent(DownloadID, remove_data)
        elif Source == "DELUGEWEBUI":
            deluge.removeTorrent(DownloadID, remove_data)
        elif Source == "DELUGERPC":
            client = DelugeRPCClient(lazylibrarian.CONFIG['DELUGE_HOST'],
                                     int(lazylibrarian.CONFIG['DELUGE_PORT']),
                                     lazylibrarian.CONFIG['DELUGE_USER'],
                                     lazylibrarian.CONFIG['DELUGE_PASS'])
            try:
                client.connect()
                client.call('core.remove_torrent', DownloadID, remove_data)
            except Exception as e:
                logger.error('DelugeRPC failed %s %s' % (type(e).__name__, str(e)))
        elif Source == 'DIRECT':
            return True
        else:
            logger.debug("Unknown source [%s] in delete_task" % Source)
            return False
        return True

    except Exception as e:
        logger.warn("Failed to delete task %s from %s: %s %s" % (DownloadID, Source, type(e).__name__, str(e)))
        return False


def import_book(pp_path=None, bookID=None):
    # noinspection PyBroadException
    try:
        # Move a book into LL folder structure given just the folder and bookID, returns True or False
        # Called from "import_alternate" or if we find a "LL.(xxx)" folder that doesn't match a snatched book/mag
        if lazylibrarian.LOGLEVEL & lazylibrarian.log_postprocess:
            logger.debug("import_book %s" % pp_path)
        if book_file(pp_path, "audiobook"):
            book_type = "AudioBook"
            dest_dir = lazylibrarian.DIRECTORY('Audio')
        elif book_file(pp_path, "ebook"):
            book_type = "eBook"
            dest_dir = lazylibrarian.DIRECTORY('eBook')
        else:
            logger.warn("Failed to find an ebook or audiobook in [%s]" % pp_path)
            return False

        myDB = database.DBConnection()
        cmd = 'SELECT AuthorName,BookName from books,authors WHERE BookID=? and books.AuthorID = authors.AuthorID'
        data = myDB.match(cmd, (bookID,))
        if data:
            cmd = 'SELECT BookID, NZBprov, AuxInfo FROM wanted WHERE BookID=? and Status="Snatched"'
            # we may have wanted to snatch an ebook and audiobook of the same title/id
            was_snatched = myDB.select(cmd, (bookID,))
            want_audio = False
            want_ebook = False
            for item in was_snatched:
                if item['AuxInfo'] == 'AudioBook':
                    want_audio = True
                elif item['AuxInfo'] == 'eBook' or item['AuxInfo'] == '':
                    want_ebook = True

            match = False
            if want_audio and book_type == "AudioBook":
                match = True
            elif want_ebook and book_type == "eBook":
                match = True
            elif not was_snatched:
                logger.debug('Bookid %s was not snatched so cannot check type, contains %s' % (bookID, book_type))
                match = True
            if not match:
                logger.debug('Bookid %s, failed to find valid %s' % (bookID, book_type))
                return False

            authorname = data['AuthorName']
            authorname = ' '.join(authorname.split())  # ensure no extra whitespace
            bookname = data['BookName']
            # DEST_FOLDER pattern is the same for ebook and audiobook
            if 'windows' in platform.system().lower() and '/' in lazylibrarian.CONFIG['EBOOK_DEST_FOLDER']:
                logger.warn('Please check your EBOOK_DEST_FOLDER setting')
                lazylibrarian.CONFIG['EBOOK_DEST_FOLDER'] = lazylibrarian.CONFIG['EBOOK_DEST_FOLDER'].replace('/', '\\')

            seriesinfo = seriesInfo(bookID)
            dest_path = lazylibrarian.CONFIG['EBOOK_DEST_FOLDER'].replace(
                '$Author', authorname).replace(
                '$Title', bookname).replace(
                '$Series', seriesinfo['Full']).replace(
                '$SerName', seriesinfo['Name']).replace(
                '$SerNum', seriesinfo['Num']).replace(
                '$$', ' ')
            dest_path = ' '.join(dest_path.split()).strip()
            dest_path = replace_all(dest_path, __dic__)
            dest_path = os.path.join(dest_dir, dest_path)
            # global_name is only used for ebooks to ensure book/cover/opf all have the same basename
            # audiobooks are usually multi part so can't be renamed this way
            global_name = lazylibrarian.CONFIG['EBOOK_DEST_FILE'].replace(
                '$Author', authorname).replace(
                '$Title', bookname).replace(
                '$Series', '').replace(
                '$SerName', '').replace(
                '$SerNum', '').replace(
                '$$', ' ')
            global_name = ' '.join(global_name.split()).strip()

            success, dest_file = processDestination(pp_path, dest_path, authorname, bookname,
                                                    global_name, bookID, book_type)
            if success:
                # update nzbs
                if was_snatched:
                    snatched_from = was_snatched[0]['NZBprov']
                    if lazylibrarian.LOGLEVEL & lazylibrarian.log_postprocess:
                        logger.debug("%s was snatched from %s" % (global_name, snatched_from))
                    controlValueDict = {"BookID": bookID}
                    newValueDict = {"Status": "Processed", "NZBDate": now()}  # say when we processed it
                    myDB.upsert("wanted", newValueDict, controlValueDict)
                else:
                    snatched_from = "manually added"
                    if lazylibrarian.LOGLEVEL & lazylibrarian.log_postprocess:
                        logger.debug("%s was %s" % (global_name, snatched_from))

                processExtras(dest_file, global_name, bookID, book_type)

                if not lazylibrarian.CONFIG['DESTINATION_COPY'] and pp_path != dest_dir:
                    if os.path.isdir(pp_path):
                        # calibre might have already deleted it?
                        try:
                            shutil.rmtree(pp_path)
                        except Exception as why:
                            logger.warn("Unable to remove %s, %s %s" % (pp_path, type(why).__name__, str(why)))
                else:
                    if lazylibrarian.CONFIG['DESTINATION_COPY']:
                        logger.debug("Not removing original files as Keep Files is set")
                    else:
                        logger.debug("Not removing original files as in download root")

                logger.info('Successfully processed: %s' % global_name)
                custom_notify_download("%s %s" % (bookID, book_type))
                if snatched_from == "manually added":
                    frm = ''
                else:
                    frm = 'from '

                notify_download("%s %s %s%s at %s" % (book_type, global_name, frm, snatched_from, now()), bookID)
                update_downloads(snatched_from)
                return True
            else:
                logger.error('Postprocessing for %s has failed: %s' % (global_name, dest_file))
                if os.path.isdir(pp_path + '.fail'):
                    try:
                        shutil.rmtree(pp_path + '.fail')
                    except Exception as why:
                        logger.warn("Unable to remove %s, %s %s" % (pp_path + '.fail', type(why).__name__, str(why)))
                try:
                    _ = safe_move(pp_path, pp_path + '.fail')
                    logger.warn('Residual files remain in %s.fail' % pp_path)
                except Exception as e:
                    logger.error("[importBook] Unable to rename %s, %s %s" % (pp_path, type(e).__name__, str(e)))
                    logger.warn('Residual files remain in %s' % pp_path)

                was_snatched = myDB.match('SELECT NZBurl FROM wanted WHERE BookID=? and Status="Snatched"', (bookID,))
                if was_snatched:
                    controlValueDict = {"NZBurl": was_snatched['NZBurl']}
                    newValueDict = {"Status": "Failed", "NZBDate": now()}
                    myDB.upsert("wanted", newValueDict, controlValueDict)
                # reset status so we try for a different version
                if book_type == 'AudioBook':
                    myDB.action('UPDATE books SET audiostatus="Wanted" WHERE BookID=?', (bookID,))
                else:
                    myDB.action('UPDATE books SET status="Wanted" WHERE BookID=?', (bookID,))
        return False
    except Exception:
        logger.error('Unhandled exception in importBook: %s' % traceback.format_exc())


def processExtras(dest_file=None, global_name=None, bookid=None, book_type="eBook"):
    # given bookid, handle author count, calibre autoadd, book image, opf

    if not bookid:
        logger.error('processExtras: No bookid supplied')
        return
    if not dest_file:
        logger.error('processExtras: No dest_file supplied')
        return

    myDB = database.DBConnection()

    controlValueDict = {"BookID": bookid}
    if book_type == 'AudioBook':
        newValueDict = {"AudioFile": dest_file, "AudioStatus": "Open", "AudioLibrary": now()}
        myDB.upsert("books", newValueDict, controlValueDict)
        if lazylibrarian.CONFIG['AUDIOBOOK_DEST_FILE'] and lazylibrarian.CONFIG['IMP_RENAME']:
            book_filename = audioRename(bookid)
            if dest_file != book_filename:
                myDB.action('UPDATE books set AudioFile=? where BookID=?', (book_filename, bookid))
    else:
        newValueDict = {"Status": "Open", "BookFile": dest_file, "BookLibrary": now()}
        myDB.upsert("books", newValueDict, controlValueDict)

    # update authors book counts
    match = myDB.match('SELECT AuthorID FROM books WHERE BookID=?', (bookid,))
    if match:
        update_totals(match['AuthorID'])

    elif book_type != 'eBook':  # only do autoadd/img/opf for ebooks
        return

    cmd = 'SELECT AuthorName,BookID,BookName,BookDesc,BookIsbn,BookImg,BookDate,BookLang,BookPub'
    cmd += ' from books,authors WHERE BookID=? and books.AuthorID = authors.AuthorID'
    data = myDB.match(cmd, (bookid,))
    if not data:
        logger.error('processExtras: No data found for bookid %s' % bookid)
        return

    dest_path = os.path.dirname(dest_file)

    # download and cache image if http link
    processIMG(dest_path, data['BookID'], data['BookImg'], global_name)

    # do we want to create metadata - there may already be one in pp_path, but it was downloaded and might
    # not contain our choice of authorname/title/identifier, so we ignore it and write our own
    if not lazylibrarian.CONFIG['IMP_AUTOADD_BOOKONLY']:
        _ = processOPF(dest_path, data, global_name, overwrite=True)

    # If you use auto add by Calibre you need the book in a single directory, not nested
    # So take the files you Copied/Moved to Dest_path and copy/move into Calibre auto add folder.
    if lazylibrarian.CONFIG['IMP_AUTOADD']:
        processAutoAdd(dest_path)


def processDestination(pp_path=None, dest_path=None, authorname=None, bookname=None, global_name=None, bookid=None,
                       booktype=None):
    """ Copy/move book/mag and associated files into target directory
        Return True, full_path_to_book  or False, error_message"""

    if not bookname:
        booktype = 'mag'

    booktype = booktype.lower()

    bestmatch = ''
    if booktype == 'ebook' and lazylibrarian.CONFIG['ONE_FORMAT']:
        booktype_list = getList(lazylibrarian.CONFIG['EBOOK_TYPE'])
        for btype in booktype_list:
            if not bestmatch:
                for fname in os.listdir(makeBytestr(pp_path)):
                    fname = makeUnicode(fname)
                    extn = os.path.splitext(fname)[1].lstrip('.')
                    if extn and extn.lower() == btype:
                        bestmatch = btype
                        break
    if bestmatch:
        match = bestmatch
        logger.debug('One format import, best match = %s' % bestmatch)
    else:  # mag or audiobook or multi-format book
        match = False
        for fname in os.listdir(makeBytestr(pp_path)):
            fname = makeUnicode(fname)
            if is_valid_booktype(fname, booktype=booktype):
                match = True
                break

    if not match:
        # no book/mag found in a format we wanted. Leave for the user to delete or convert manually
        return False, 'Unable to locate a valid filetype (%s) in %s, leaving for manual processing' % (
            booktype, pp_path)

    # If ebook, do we want calibre to import the book for us
    newbookfile = ''
    if booktype == 'ebook' and len(lazylibrarian.CONFIG['IMP_CALIBREDB']):
        dest_dir = lazylibrarian.DIRECTORY('eBook')
        try:
            logger.debug('Importing %s into calibre library' % global_name)
            # calibre may ignore metadata.opf and book_name.opf depending on calibre settings,
            # and ignores opf data if there is data embedded in the book file
            # so we send separate "set_metadata" commands after the import
            for fname in os.listdir(makeBytestr(pp_path)):
                fname = makeUnicode(fname)
                filename, extn = os.path.splitext(fname)
                srcfile = os.path.join(pp_path, fname)
                if is_valid_booktype(fname, booktype=booktype) or extn in ['.opf', '.jpg']:
                    if bestmatch and not fname.endswith(bestmatch) and extn not in ['.opf', '.jpg']:
                        logger.debug("Removing %s as not %s" % (fname, bestmatch))
                        os.remove(srcfile)
                    else:
                        dstfile = os.path.join(pp_path, global_name.replace('"', '_') + extn)
                        # calibre does not like quotes in author names
                        _ = safe_move(srcfile, dstfile)
                else:
                    logger.debug('Removing %s as not wanted' % fname)
                    os.remove(srcfile)
            if bookid.isdigit():
                identifier = "goodreads:%s" % bookid
            else:
                identifier = "google:%s" % bookid

            res, err, rc = calibredb('add', ['-1'], [pp_path])

            if rc:
                return False, 'calibredb rc %s from %s' % (rc, lazylibrarian.CONFIG['IMP_CALIBREDB'])
            elif 'already exist' in err or 'already exist' in res:  # needed for different calibredb versions
                return False, 'Calibre failed to import %s %s, already exists' % (authorname, bookname)
            elif 'Added book ids' not in res:
                return False, 'Calibre failed to import %s %s, no added bookids' % (authorname, bookname)

            calibre_id = res.split("book ids: ", 1)[1].split("\n", 1)[0]
            logger.debug('Calibre ID: %s' % calibre_id)

            our_opf = False
            rc = 0
            if not lazylibrarian.CONFIG['IMP_AUTOADD_BOOKONLY']:
                # we can pass an opf with all the info, and a cover image
                myDB = database.DBConnection()
                cmd = 'SELECT AuthorName,BookID,BookName,BookDesc,BookIsbn,BookImg,BookDate,BookLang,BookPub'
                cmd += ' from books,authors WHERE BookID=? and books.AuthorID = authors.AuthorID'
                data = myDB.match(cmd, (bookid,))
                if not data:
                    logger.error('processDestination: No data found for bookid %s' % bookid)
                else:
                    processIMG(pp_path, data['BookID'], data['BookImg'], global_name)
                    opfpath, our_opf = processOPF(pp_path, data, global_name, True)
                    _, _, rc = calibredb('set_metadata', None, [calibre_id, opfpath])
                if rc:
                    logger.warn("calibredb unable to set opf")

            if not our_opf and not rc:  # pre-existing opf might not have our preferred authorname/title/identifier
                _, _, rc = calibredb('set_metadata', ['--field', 'authors:%s' % unaccented(authorname)], [calibre_id])
                if rc:
                    logger.warn("calibredb unable to set author")
                _, _, rc = calibredb('set_metadata', ['--field', 'title:%s' % unaccented(bookname)], [calibre_id])
                if rc:
                    logger.warn("calibredb unable to set title")
                _, _, rc = calibredb('set_metadata', ['--field', 'identifiers:%s' % identifier], [calibre_id])
                if rc:
                    logger.warn("calibredb unable to set identifier")

            # calibre does not like accents or quotes in names
            if authorname.endswith('.'):  # calibre replaces trailing dot with underscore eg Jr. becomes Jr_
                authorname = authorname[:-1] + '_'
            calibre_dir = os.path.join(dest_dir, unaccented_str(authorname.replace('"', '_')), '')
            if os.path.isdir(calibre_dir):  # assumed author directory
                target_dir = os.path.join(calibre_dir, '%s (%s)' % (unaccented(bookname), calibre_id))
                logger.debug('Calibre trying directory [%s]' % target_dir)
                remove = bool(lazylibrarian.CONFIG['FULL_SCAN'])
                if os.path.isdir(target_dir):
                    _ = LibraryScan(target_dir, remove=remove)
                    newbookfile = book_file(target_dir, booktype='ebook')
                    # should we be setting permissions on calibres directories and files?
                    if newbookfile:
                        setperm(target_dir)
                        for fname in os.listdir(makeBytestr(target_dir)):
                            fname = makeUnicode(fname)
                            setperm(os.path.join(target_dir, fname))
                        return True, newbookfile
                    return False, "Failed to find a valid ebook in [%s]" % target_dir
                else:
                    _ = LibraryScan(calibre_dir, remove=remove)  # rescan whole authors directory
                    myDB = database.DBConnection()
                    match = myDB.match('SELECT BookFile FROM books WHERE BookID=?', (bookid,))
                    if match:
                        return True, match['BookFile']
                    return False, 'Failed to find bookfile for %s in database' % bookid
            return False, "Failed to locate calibre author dir [%s]" % calibre_dir
            # imported = LibraryScan(dest_dir)  # may have to rescan whole library instead
        except Exception as e:
            return False, 'calibredb import failed, %s %s' % (type(e).__name__, str(e))
    else:
        # we are copying the files ourselves, either it's audiobook, magazine or we don't want to use calibre
        if lazylibrarian.LOGLEVEL & lazylibrarian.log_postprocess:
            logger.debug("BookType: %s, calibredb: [%s]" % (booktype, lazylibrarian.CONFIG['IMP_CALIBREDB']))
        if not os.path.exists(dest_path):
            logger.debug('%s does not exist, so it\'s safe to create it' % dest_path)
        elif not os.path.isdir(dest_path):
            logger.debug('%s exists but is not a directory, deleting it' % dest_path)
            try:
                os.remove(dest_path)
            except OSError as why:
                return False, 'Unable to delete %s: %s' % (dest_path, why.strerror)
        if os.path.isdir(dest_path):
            setperm(dest_path)
        else:
            res = mymakedirs(dest_path)
            if not res:
                return False, 'Unable to create directory %s' % dest_path

        # ok, we've got a target directory, try to copy only the files we want, renaming them on the fly.
        firstfile = ''  # try to keep track of "preferred" ebook type or the first part of multi-part audiobooks
        for fname in os.listdir(makeBytestr(pp_path)):
            fname = makeUnicode(fname)
            if bestmatch and is_valid_booktype(fname, booktype=booktype) and not fname.endswith(bestmatch):
                logger.debug("Ignoring %s as not %s" % (fname, bestmatch))
            else:
                if is_valid_booktype(fname, booktype=booktype) or \
                        ((fname.lower().endswith(".jpg") or fname.lower().endswith(".opf"))
                         and not lazylibrarian.CONFIG['IMP_AUTOADD_BOOKONLY']):
                    logger.debug('Copying %s to directory %s' % (fname, dest_path))
                    typ = ''
                    srcfile = os.path.join(pp_path, fname)
                    if booktype == 'audiobook':
                        destfile = os.path.join(dest_path, fname)  # don't rename, just copy it
                    else:
                        # for ebooks, the book, jpg, opf all have the same basename
                        destfile = os.path.join(dest_path, global_name + os.path.splitext(fname)[1])
                    try:
                        if lazylibrarian.CONFIG['DESTINATION_COPY']:
                            typ = 'copy'
                            destfile = safe_copy(srcfile, destfile)
                        else:
                            typ = 'move'
                            destfile = safe_move(srcfile, destfile)
                        setperm(destfile)
                        if is_valid_booktype(destfile, booktype=booktype):
                            newbookfile = destfile
                    except Exception as why:
                        return False, "Unable to %s file %s to %s: %s %s" % \
                               (typ, srcfile, destfile, type(why).__name__, str(why))
                else:
                    logger.debug('Ignoring unwanted file: %s' % fname)

        # for ebooks, prefer the first book_type found in ebook_type list
        if booktype == 'ebook':
            book_basename = os.path.join(dest_path, global_name)
            booktype_list = getList(lazylibrarian.CONFIG['EBOOK_TYPE'])
            for book_type in booktype_list:
                preferred_type = "%s.%s" % (book_basename, book_type)
                if os.path.exists(preferred_type):
                    logger.debug("Link to preferred type %s, %s" % (book_type, preferred_type))
                    firstfile = preferred_type
                    break

        # link to the first part of multi-part audiobooks
        elif booktype == 'audiobook':
            tokmatch = ''
            for token in [' 001.', ' 01.', ' 1.', ' 001 ', ' 01 ', ' 1 ', '01']:
                if tokmatch:
                    break
                for f in os.listdir(makeBytestr(pp_path)):
                    f = makeUnicode(f)
                    if is_valid_booktype(f, booktype='audiobook') and token in f:
                        firstfile = os.path.join(pp_path, f)
                        logger.debug("Link to preferred part [%s], %s" % (token, f))
                        tokmatch = token
                        break
        if firstfile:
            newbookfile = firstfile
    return True, newbookfile


def processAutoAdd(src_path=None, booktype='book'):
    # Called to copy/move the book files to an auto add directory for the likes of Calibre which can't do nested dirs
    autoadddir = lazylibrarian.CONFIG['IMP_AUTOADD']
    if booktype == 'mag':
        autoadddir = lazylibrarian.CONFIG['IMP_AUTOADDMAG']

    if not os.path.exists(autoadddir):
        logger.error('AutoAdd directory for %s [%s] is missing or not set - cannot perform autoadd' % (
            booktype, autoadddir))
        return False
    # Now try and copy all the book files into a single dir.
    try:
        names = os.listdir(makeBytestr(src_path))
        names = [makeUnicode(item) for item in names]
        # files jpg, opf & book(s) should have same name
        # Caution - book may be pdf, mobi, epub or all 3.
        # for now simply copy all files, and let the autoadder sort it out
        #
        # Update - seems Calibre will only use the jpeg if named same as book, not cover.jpg
        # and only imports one format of each ebook, treats the others as duplicates, might be configable in calibre?
        # ignores author/title data in opf file if there is any embedded in book

        match = False
        if booktype == 'book' and lazylibrarian.CONFIG['ONE_FORMAT']:
            booktype_list = getList(lazylibrarian.CONFIG['EBOOK_TYPE'])
            for booktype in booktype_list:
                while not match:
                    for name in names:
                        extn = os.path.splitext(name)[1].lstrip('.')
                        if extn and extn.lower() == booktype:
                            match = booktype
                            break
        copied = False
        for name in names:
            if match and is_valid_booktype(name, booktype=booktype) and not name.endswith(match):
                logger.debug('Skipping %s' % os.path.splitext(name)[1])
            elif booktype == 'book' and lazylibrarian.CONFIG['IMP_AUTOADD_BOOKONLY'] and not \
                    is_valid_booktype(name, booktype="book"):
                logger.debug('Skipping %s' % name)
            elif booktype == 'mag' and lazylibrarian.CONFIG['IMP_AUTOADD_MAGONLY'] and not \
                    is_valid_booktype(name, booktype="mag"):
                logger.debug('Skipping %s' % name)
            else:
                srcname = os.path.join(src_path, name)
                dstname = os.path.join(autoadddir, name)
                try:
                    if lazylibrarian.CONFIG['DESTINATION_COPY']:
                        logger.debug('AutoAdd Copying file [%s] from [%s] to [%s]' % (name, srcname, dstname))
                        dstname = safe_copy(srcname, dstname)
                    else:
                        logger.debug('AutoAdd Moving file [%s] from [%s] to [%s]' % (name, srcname, dstname))
                        dstname = safe_move(srcname, dstname)
                    copied = True
                except Exception as why:
                    logger.error('AutoAdd - Failed to copy/move file [%s] %s [%s] ' %
                                 (name, type(why).__name__, str(why)))
                    return False
                try:
                    os.chmod(dstname, 0o666)  # make rw for calibre
                except OSError as why:
                    logger.warn("Could not set permission of %s because [%s]" % (dstname, why.strerror))
                    # permissions might not be fatal, continue

        if copied and not lazylibrarian.CONFIG['DESTINATION_COPY']:  # do we want to keep the original files?
            logger.debug('Removing %s' % src_path)
            shutil.rmtree(src_path)

    except OSError as why:
        logger.error('AutoAdd - Failed because [%s]' % why.strerror)
        return False

    logger.info('Auto Add completed for [%s]' % src_path)
    return True


def processIMG(dest_path=None, bookid=None, bookimg=None, global_name=None):
    """ cache the bookimg from url or filename, and optionally copy it to bookdir """
    if lazylibrarian.CONFIG['IMP_AUTOADD_BOOKONLY']:
        logger.debug('Not creating coverfile, bookonly is set')
        return

    jpgfile = jpg_file(dest_path)
    if jpgfile:
        logger.debug('Cover %s already exists' % jpgfile)
        setperm(jpgfile)
        return

    link, success, _ = cache_img('book', bookid, bookimg, False)
    if not success:
        logger.error('Error caching cover from %s, %s' % (bookimg, link))
        return

    cachefile = os.path.join(lazylibrarian.CACHEDIR, 'book', bookid + '.jpg')
    coverfile = os.path.join(dest_path, global_name + '.jpg')
    try:
        coverfile = safe_copy(cachefile, coverfile)
        setperm(coverfile)
    except Exception as e:
        logger.error("Error copying image to %s, %s %s" % (coverfile, type(e).__name__, str(e)))
        return


def processMAGOPF(issuefile, title, issue, issueID, overwrite=False):
    """ Needs calibre to be configured to read metadata from file contents, not filename """
    if not lazylibrarian.CONFIG['IMP_MAGOPF']:
        return
    dest_path, global_name = os.path.split(issuefile)
    global_name, extn = os.path.splitext(global_name)

    if len(issue) == 10 and issue[8:] == '01' and issue[4] == '-' and issue[7] == '-':  # yyyy-mm-01
        yr = issue[0:4]
        mn = issue[5:7]
        month = lazylibrarian.MONTHNAMES[int(mn)][0]
        iname = "%s - %s%s %s" % (title, month[0].upper(), month[1:], yr)  # The Magpi - January 2017
    elif title in issue:
        iname = issue  # 0063 - Android Magazine -> 0063
    else:
        iname = "%s - %s" % (title, issue)  # Android Magazine - 0063

    mtime = os.path.getmtime(issuefile)
    iss_acquired = datetime.date.isoformat(datetime.date.fromtimestamp(mtime))

    data = {
        'AuthorName': title,
        'BookID': issueID,
        'BookName': iname,
        'BookDesc': '',
        'BookIsbn': '',
        'BookDate': iss_acquired,
        'BookLang': 'eng',
        'BookImg': global_name + '.jpg',
        'BookPub': '',
        'Series': title,
        'Series_index': issue
    }  # type: dict
    # noinspection PyTypeChecker
    _ = processOPF(dest_path, data, global_name, overwrite=overwrite)


def processOPF(dest_path=None, data=None, global_name=None, overwrite=False):
    opfpath = os.path.join(dest_path, global_name + '.opf')
    if not overwrite and os.path.exists(opfpath):
        logger.debug('%s already exists. Did not create one.' % opfpath)
        setperm(opfpath)
        return opfpath, False

    bookid = data['BookID']
    if bookid.isdigit():
        scheme = 'GOODREADS'
    else:
        scheme = 'GoogleBooks'

    seriesname = ''
    seriesnum = ''
    if 'Series_index' not in data:
        # no series details passed in data dictionary, look them up in db
        myDB = database.DBConnection()
        if scheme == 'GOODREADS' and 'WorkID' in data and data['WorkID']:
            cmd = 'SELECT SeriesID,SeriesNum from member WHERE workid=?'
            res = myDB.match(cmd, (data['WorkID'],))
        else:
            cmd = 'SELECT SeriesID,SeriesNum from member WHERE bookid=?'
            res = myDB.match(cmd, (bookid,))
        if res:
            seriesid = res['SeriesID']
            serieslist = getList(res['SeriesNum'])
            # might be "Book 3.5" or similar, just get the numeric part
            while serieslist:
                seriesnum = serieslist.pop()
                try:
                    _ = float(seriesnum)
                    break
                except ValueError:
                    seriesnum = ''
                    pass

            if not seriesnum:
                # couldn't figure out number, keep everything we got, could be something like "Book Two"
                serieslist = res['SeriesNum']

            cmd = 'SELECT SeriesName from series WHERE seriesid=?'
            res = myDB.match(cmd, (seriesid,))
            if res:
                seriesname = res['SeriesName']
                if not seriesnum:
                    # add what we got to series name and set seriesnum to 1 so user can sort it out manually
                    seriesname = "%s %s" % (seriesname, serieslist)
                    seriesnum = 1

    opfinfo = '<?xml version="1.0"  encoding="UTF-8"?>\n\
<package version="2.0" xmlns="http://www.idpf.org/2007/opf" >\n\
    <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">\n\
        <dc:title>%s</dc:title>\n\
        <dc:creator opf:file-as="%s" opf:role="aut">%s</dc:creator>\n\
        <dc:language>%s</dc:language>\n\
        <dc:identifier scheme="%s">%s</dc:identifier>\n' % (data['BookName'], surnameFirst(data['AuthorName']),
                                                            data['AuthorName'], data['BookLang'], scheme, bookid)

    if 'BookIsbn' in data:
        opfinfo += '        <dc:identifier scheme="ISBN">%s</dc:identifier>\n' % data['BookIsbn']

    if 'BookPub' in data:
        opfinfo += '        <dc:publisher>%s</dc:publisher>\n' % data['BookPub']

    if 'BookDate' in data:
        opfinfo += '        <dc:date>%s</dc:date>\n' % data['BookDate']

    if 'BookDesc' in data:
        opfinfo += '        <dc:description>%s</dc:description>\n' % data['BookDesc']

    if seriesname:
        opfinfo += '        <meta content="%s" name="calibre:series"/>\n' % seriesname
    elif 'Series' in data:
        opfinfo += '        <meta content="%s" name="calibre:series"/>\n' % data['Series']

    if seriesnum:
        opfinfo += '        <meta content="%s" name="calibre:series_index"/>\n' % seriesnum
    elif 'Series_index' in data:
        opfinfo += '        <meta content="%s" name="calibre:series_index"/>\n' % data['Series_index']

    opfinfo += '        <guide>\n\
            <reference href="%s.jpg" type="cover" title="Cover"/>\n\
        </guide>\n\
    </metadata>\n\
</package>' % global_name  # file in current directory, not full path

    dic = {'...': '', ' & ': ' ', ' = ': ' ', '$': 's', ' + ': ' ', '*': ''}

    opfinfo = unaccented_str(replace_all(opfinfo, dic))

    if PY2:
        fmode = 'wb'
    else:
        fmode = 'w'
    with open(opfpath, fmode) as opf:
        opf.write(opfinfo)
    logger.debug('Saved metadata to: ' + opfpath)
    setperm(opfpath)
    return opfpath, True
