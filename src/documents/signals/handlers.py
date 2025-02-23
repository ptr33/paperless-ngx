import logging
import os
import shutil
from ast import literal_eval
from pathlib import Path

from celery import states
from celery.signals import before_task_publish
from celery.signals import task_postrun
from celery.signals import task_prerun
from django.conf import settings
from django.contrib.admin.models import ADDITION
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.db import DatabaseError
from django.db import models
from django.db.models import Q
from django.dispatch import receiver
from django.utils import termcolors
from django.utils import timezone
from filelock import FileLock

from .. import matching
from ..file_handling import create_source_path_directory
from ..file_handling import delete_empty_directories
from ..file_handling import generate_unique_filename
from ..models import Document
from ..models import MatchingModel
from ..models import PaperlessTask
from ..models import Tag

logger = logging.getLogger("paperless.handlers")


def add_inbox_tags(sender, document=None, logging_group=None, **kwargs):
    inbox_tags = Tag.objects.filter(is_inbox_tag=True)
    document.tags.add(*inbox_tags)


def set_correspondent(
    sender,
    document=None,
    logging_group=None,
    classifier=None,
    replace=False,
    use_first=True,
    suggest=False,
    base_url=None,
    color=False,
    **kwargs,
):
    if document.correspondent and not replace:
        return

    potential_correspondents = matching.match_correspondents(document, classifier)

    potential_count = len(potential_correspondents)
    if potential_correspondents:
        selected = potential_correspondents[0]
    else:
        selected = None
    if potential_count > 1:
        if use_first:
            logger.debug(
                f"Detected {potential_count} potential correspondents, "
                f"so we've opted for {selected}",
                extra={"group": logging_group},
            )
        else:
            logger.debug(
                f"Detected {potential_count} potential correspondents, "
                f"not assigning any correspondent",
                extra={"group": logging_group},
            )
            return

    if selected or replace:
        if suggest:
            if base_url:
                print(
                    termcolors.colorize(str(document), fg="green")
                    if color
                    else str(document),
                )
                print(f"{base_url}/documents/{document.pk}")
            else:
                print(
                    (
                        termcolors.colorize(str(document), fg="green")
                        if color
                        else str(document)
                    )
                    + f" [{document.pk}]",
                )
            print(f"Suggest correspondent {selected}")
        else:
            logger.info(
                f"Assigning correspondent {selected} to {document}",
                extra={"group": logging_group},
            )

            document.correspondent = selected
            document.save(update_fields=("correspondent",))


def set_document_type(
    sender,
    document=None,
    logging_group=None,
    classifier=None,
    replace=False,
    use_first=True,
    suggest=False,
    base_url=None,
    color=False,
    **kwargs,
):
    if document.document_type and not replace:
        return

    potential_document_type = matching.match_document_types(document, classifier)

    potential_count = len(potential_document_type)
    if potential_document_type:
        selected = potential_document_type[0]
    else:
        selected = None

    if potential_count > 1:
        if use_first:
            logger.info(
                f"Detected {potential_count} potential document types, "
                f"so we've opted for {selected}",
                extra={"group": logging_group},
            )
        else:
            logger.info(
                f"Detected {potential_count} potential document types, "
                f"not assigning any document type",
                extra={"group": logging_group},
            )
            return

    if selected or replace:
        if suggest:
            if base_url:
                print(
                    termcolors.colorize(str(document), fg="green")
                    if color
                    else str(document),
                )
                print(f"{base_url}/documents/{document.pk}")
            else:
                print(
                    (
                        termcolors.colorize(str(document), fg="green")
                        if color
                        else str(document)
                    )
                    + f" [{document.pk}]",
                )
            print(f"Suggest document type {selected}")
        else:
            logger.info(
                f"Assigning document type {selected} to {document}",
                extra={"group": logging_group},
            )

            document.document_type = selected
            document.save(update_fields=("document_type",))


def set_tags(
    sender,
    document=None,
    logging_group=None,
    classifier=None,
    replace=False,
    suggest=False,
    base_url=None,
    color=False,
    **kwargs,
):

    if replace:
        Document.tags.through.objects.filter(document=document).exclude(
            Q(tag__is_inbox_tag=True),
        ).exclude(
            Q(tag__match="") & ~Q(tag__matching_algorithm=Tag.MATCH_AUTO),
        ).delete()

    current_tags = set(document.tags.all())

    matched_tags = matching.match_tags(document, classifier)

    relevant_tags = set(matched_tags) - current_tags

    if suggest:
        extra_tags = current_tags - set(matched_tags)
        extra_tags = [
            t for t in extra_tags if t.matching_algorithm == MatchingModel.MATCH_AUTO
        ]
        if not relevant_tags and not extra_tags:
            return
        if base_url:
            print(
                termcolors.colorize(str(document), fg="green")
                if color
                else str(document),
            )
            print(f"{base_url}/documents/{document.pk}")
        else:
            print(
                (
                    termcolors.colorize(str(document), fg="green")
                    if color
                    else str(document)
                )
                + f" [{document.pk}]",
            )
        if relevant_tags:
            print("Suggest tags: " + ", ".join([t.name for t in relevant_tags]))
        if extra_tags:
            print("Extra tags: " + ", ".join([t.name for t in extra_tags]))
    else:
        if not relevant_tags:
            return

        message = 'Tagging "{}" with "{}"'
        logger.info(
            message.format(document, ", ".join([t.name for t in relevant_tags])),
            extra={"group": logging_group},
        )

        document.tags.add(*relevant_tags)


def set_storage_path(
    sender,
    document=None,
    logging_group=None,
    classifier=None,
    replace=False,
    use_first=True,
    suggest=False,
    base_url=None,
    color=False,
    **kwargs,
):
    if document.storage_path and not replace:
        return

    potential_storage_path = matching.match_storage_paths(
        document,
        classifier,
    )

    potential_count = len(potential_storage_path)
    if potential_storage_path:
        selected = potential_storage_path[0]
    else:
        selected = None

    if potential_count > 1:
        if use_first:
            logger.info(
                f"Detected {potential_count} potential storage paths, "
                f"so we've opted for {selected}",
                extra={"group": logging_group},
            )
        else:
            logger.info(
                f"Detected {potential_count} potential storage paths, "
                f"not assigning any storage directory",
                extra={"group": logging_group},
            )
            return

    if selected or replace:
        if suggest:
            if base_url:
                print(
                    termcolors.colorize(str(document), fg="green")
                    if color
                    else str(document),
                )
                print(f"{base_url}/documents/{document.pk}")
            else:
                print(
                    (
                        termcolors.colorize(str(document), fg="green")
                        if color
                        else str(document)
                    )
                    + f" [{document.pk}]",
                )
            print(f"Suggest storage directory {selected}")
        else:
            logger.info(
                f"Assigning storage path {selected} to {document}",
                extra={"group": logging_group},
            )

            document.storage_path = selected
            document.save(update_fields=("storage_path",))


@receiver(models.signals.post_delete, sender=Document)
def cleanup_document_deletion(sender, instance, using, **kwargs):
    with FileLock(settings.MEDIA_LOCK):
        if settings.TRASH_DIR:
            # Find a non-conflicting filename in case a document with the same
            # name was moved to trash earlier
            counter = 0
            old_filename = os.path.split(instance.source_path)[1]
            (old_filebase, old_fileext) = os.path.splitext(old_filename)

            while True:
                new_file_path = os.path.join(
                    settings.TRASH_DIR,
                    old_filebase + (f"_{counter:02}" if counter else "") + old_fileext,
                )

                if os.path.exists(new_file_path):
                    counter += 1
                else:
                    break

            logger.debug(f"Moving {instance.source_path} to trash at {new_file_path}")
            try:
                shutil.move(instance.source_path, new_file_path)
            except OSError as e:
                logger.error(
                    f"Failed to move {instance.source_path} to trash at "
                    f"{new_file_path}: {e}. Skipping cleanup!",
                )
                return

        for filename in (
            instance.source_path,
            instance.archive_path,
            instance.thumbnail_path,
        ):
            if filename and os.path.isfile(filename):
                try:
                    os.unlink(filename)
                    logger.debug(f"Deleted file {filename}.")
                except OSError as e:
                    logger.warning(
                        f"While deleting document {str(instance)}, the file "
                        f"{filename} could not be deleted: {e}",
                    )

        delete_empty_directories(
            os.path.dirname(instance.source_path),
            root=settings.ORIGINALS_DIR,
        )

        if instance.has_archive_version:
            delete_empty_directories(
                os.path.dirname(instance.archive_path),
                root=settings.ARCHIVE_DIR,
            )


class CannotMoveFilesException(Exception):
    pass


def validate_move(instance, old_path, new_path):
    if not os.path.isfile(old_path):
        # Can't do anything if the old file does not exist anymore.
        logger.fatal(f"Document {str(instance)}: File {old_path} has gone.")
        raise CannotMoveFilesException()

    if os.path.isfile(new_path):
        # Can't do anything if the new file already exists. Skip updating file.
        logger.warning(
            f"Document {str(instance)}: Cannot rename file "
            f"since target path {new_path} already exists.",
        )
        raise CannotMoveFilesException()


@receiver(models.signals.m2m_changed, sender=Document.tags.through)
@receiver(models.signals.post_save, sender=Document)
def update_filename_and_move_files(sender, instance, **kwargs):

    if not instance.filename:
        # Can't update the filename if there is no filename to begin with
        # This happens when the consumer creates a new document.
        # The document is modified and saved multiple times, and only after
        # everything is done (i.e., the generated filename is final),
        # filename will be set to the location where the consumer has put
        # the file.
        #
        # This will in turn cause this logic to move the file where it belongs.
        return

    with FileLock(settings.MEDIA_LOCK):
        try:

            # If this was waiting for the lock, the filename or archive_filename
            # of this document may have been updated.  This happens if multiple updates
            # get queued from the UI for the same document
            # So freshen up the data before doing anything
            instance.refresh_from_db()

            old_filename = instance.filename
            old_source_path = instance.source_path

            instance.filename = generate_unique_filename(instance)
            move_original = old_filename != instance.filename

            old_archive_filename = instance.archive_filename
            old_archive_path = instance.archive_path

            if instance.has_archive_version:

                instance.archive_filename = generate_unique_filename(
                    instance,
                    archive_filename=True,
                )

                move_archive = old_archive_filename != instance.archive_filename
            else:
                move_archive = False

            if not move_original and not move_archive:
                # Don't do anything if filenames did not change.
                return

            if move_original:
                validate_move(instance, old_source_path, instance.source_path)
                create_source_path_directory(instance.source_path)
                os.rename(old_source_path, instance.source_path)

            if move_archive:
                validate_move(instance, old_archive_path, instance.archive_path)
                create_source_path_directory(instance.archive_path)
                os.rename(old_archive_path, instance.archive_path)

            # Don't save() here to prevent infinite recursion.
            Document.objects.filter(pk=instance.pk).update(
                filename=instance.filename,
                archive_filename=instance.archive_filename,
            )

        except (OSError, DatabaseError, CannotMoveFilesException):
            # This happens when either:
            #  - moving the files failed due to file system errors
            #  - saving to the database failed due to database errors
            # In both cases, we need to revert to the original state.

            # Try to move files to their original location.
            try:
                if move_original and os.path.isfile(instance.source_path):
                    os.rename(instance.source_path, old_source_path)

                if move_archive and os.path.isfile(instance.archive_path):
                    os.rename(instance.archive_path, old_archive_path)

            except Exception:
                # This is fine, since:
                # A: if we managed to move source from A to B, we will also
                #  manage to move it from B to A. If not, we have a serious
                #  issue that's going to get caught by the santiy checker.
                #  All files remain in place and will never be overwritten,
                #  so this is not the end of the world.
                # B: if moving the orignal file failed, nothing has changed
                #  anyway.
                pass

            # restore old values on the instance
            instance.filename = old_filename
            instance.archive_filename = old_archive_filename

        # finally, remove any empty sub folders. This will do nothing if
        # something has failed above.
        if not os.path.isfile(old_source_path):
            delete_empty_directories(
                os.path.dirname(old_source_path),
                root=settings.ORIGINALS_DIR,
            )

        if instance.has_archive_version and not os.path.isfile(
            old_archive_path,
        ):
            delete_empty_directories(
                os.path.dirname(old_archive_path),
                root=settings.ARCHIVE_DIR,
            )


def set_log_entry(sender, document=None, logging_group=None, **kwargs):

    ct = ContentType.objects.get(model="document")
    user = User.objects.get(username="consumer")

    LogEntry.objects.create(
        action_flag=ADDITION,
        action_time=timezone.now(),
        content_type=ct,
        object_id=document.pk,
        user=user,
        object_repr=document.__str__(),
    )


def add_to_index(sender, document, **kwargs):
    from documents import index

    index.add_or_update_document(document)


@before_task_publish.connect
def before_task_publish_handler(sender=None, headers=None, body=None, **kwargs):
    """
    Creates the PaperlessTask object in a pending state.  This is sent before
    the task reaches the broker, but

    https://docs.celeryq.dev/en/stable/userguide/signals.html#before-task-publish

    """
    if "task" not in headers or headers["task"] != "documents.tasks.consume_file":
        # Assumption: this is only ever a v2 message
        return

    try:
        task_file_name = ""
        if headers["kwargsrepr"] is not None:
            task_kwargs = literal_eval(headers["kwargsrepr"])
            if "override_filename" in task_kwargs:
                task_file_name = task_kwargs["override_filename"]
        else:
            task_kwargs = None

        task_args = literal_eval(headers["argsrepr"])

        # Nothing was found, report the task first argument
        if not len(task_file_name):
            # There are always some arguments to the consume, first is always filename
            filepath = Path(task_args[0])
            task_file_name = filepath.name

        PaperlessTask.objects.create(
            task_id=headers["id"],
            status=states.PENDING,
            task_file_name=task_file_name,
            task_name=headers["task"],
            task_args=task_args,
            task_kwargs=task_kwargs,
            result=None,
            date_created=timezone.now(),
            date_started=None,
            date_done=None,
        )
    except Exception as e:  # pragma: no cover
        # Don't let an exception in the signal handlers prevent
        # a document from being consumed.
        logger.error(f"Creating PaperlessTask failed: {e}")


@task_prerun.connect
def task_prerun_handler(sender=None, task_id=None, task=None, **kwargs):
    """

    Updates the PaperlessTask to be started.  Sent before the task begins execution
    on a worker.

    https://docs.celeryq.dev/en/stable/userguide/signals.html#task-prerun
    """
    try:
        task_instance = PaperlessTask.objects.filter(task_id=task_id).first()

        if task_instance is not None:
            task_instance.status = states.STARTED
            task_instance.date_started = timezone.now()
            task_instance.save()
    except Exception as e:  # pragma: no cover
        # Don't let an exception in the signal handlers prevent
        # a document from being consumed.
        logger.error(f"Setting PaperlessTask started failed: {e}")


@task_postrun.connect
def task_postrun_handler(
    sender=None, task_id=None, task=None, retval=None, state=None, **kwargs
):
    """
    Updates the result of the PaperlessTask.

    https://docs.celeryq.dev/en/stable/userguide/signals.html#task-postrun
    """
    try:
        task_instance = PaperlessTask.objects.filter(task_id=task_id).first()

        if task_instance is not None:
            task_instance.status = state
            task_instance.result = retval
            task_instance.date_done = timezone.now()
            task_instance.save()
    except Exception as e:  # pragma: no cover
        # Don't let an exception in the signal handlers prevent
        # a document from being consumed.
        logger.error(f"Updating PaperlessTask failed: {e}")
