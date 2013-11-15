from askbot.models import BadgeData
from askbot.models import FavoriteQuestion
from askbot.models import Group
from askbot.models import ImportRun
from askbot.models import ImportedObjectInfo
from askbot.models import Message
from askbot.models import Post
from askbot.models import Tag
from askbot.models import Thread
from askbot.models import User
from bs4 import BeautifulSoup
from django.conf import settings as django_settings
from django.contrib.auth.models import Group as AuthGroup
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError
from django.core import serializers
from django.db import transaction
from django.db.models import Q
from django.utils.encoding import smart_str
from django.utils.translation import activate as activate_language
import os
import sys
from tempfile import mkstemp

if 'avatar' in django_settings.INSTALLED_APPS:
    from avatar.models import Avatar

def get_status_rank(status):
    """returns integer rank of user account status,
    the larger is the number the higher is the status"""
    if len(status) != 1:
        #default status - approved user
        status = 'a'
    try:
        return 'bswamd'.index(status)
    except ValueError:
        return 0

def get_safe_username(username):
    """get unique username similar to `username`
    to avoid the uniqueness clash"""
    existing_names = User.objects.filter(
                    username__istartswith=username
                ).values_list('username', flat=True)

    if len(existing_names) == 0:
        return username

    num = 1
    while True:
        new_name = username + str(num)
        if new_name in existing_names:
            num += 1
        else:
            return new_name

def get_deserialized_object(xml_soup):
    """returns deserialized django object for xml soup with one item"""
    item_xml = smart_str(xml_soup)
    #below call assumes a single item within
    obj = serializers.deserialize('xml', item_xml).next().object
    obj._source_xml = item_xml
    return obj

def get_m2m_ids_for_field(obj, field_name):
    xml = obj._source_xml
    soup = BeautifulSoup(xml)
    ids = list()
    for field in soup.findAll('field', attrs={'name': field_name}): 
        objs = field.findAll('object')
        for obj in objs:
            ids.append(obj.attrs['pk'])
    return ids

def copy_string_parameter(from_obj, to_obj, param_name):
    from_par = getattr(from_obj, param_name)
    to_par = getattr(to_obj, param_name)
    if from_par is None and to_par is None:
        return
    from_par = from_par or ''
    to_par = to_par or ''
    if from_par.strip() == '' and to_par.strip() != '':
        setattr(to_obj, param_name, from_par)

def copy_bool_parameter(from_obj, to_obj, param_name, operator='or'):
    from_par = getattr(from_obj, param_name)
    to_par = getattr(to_obj, param_name)
    if operator == 'or':
        value = from_par or to_par
    elif operator == 'and':
        value = from_par and to_par
    else:
        raise ValueError('unsupported operator "%s"' % operator)
    setattr(to_obj, param_name, value)

def merge_words_parameter(from_obj, to_obj, param_name):
    from_words = getattr(from_obj, param_name).split()
    to_words = getattr(to_obj, param_name).split()
    value = ' '.join(set(from_words)|set(to_words))
    setattr(to_obj, param_name, value)

def copy_numeric_parameter(from_obj, to_obj, param_name, operator='max'):
    from_par = getattr(from_obj, param_name)
    to_par = getattr(to_obj, param_name)
    if operator == 'max':
        value = max(from_par, to_par)
    elif operator == 'min':
        value = min(from_par, to_par)
    elif operator == 'sum':
        value =  from_par + to_par
    else:
        raise ValueError('unsupported operator "%s"' % operator)
    setattr(to_obj, param_name, value)

class Command(BaseCommand):
    help = 'Adds XML askbot data produced by the "dumpdata" command'

    def handle(self, *args, **kwargs):

        activate_language(django_settings.LANGUAGE_CODE)

        self.setup_run()
        self.read_xml_file(args[0])
        self.remember_message_ids()
        self.read_content_types()

        self.import_groups()
        self.import_users()
        #we don't import subscriptions
        if 'avatar' in django_settings.INSTALLED_APPS:
            self.import_avatars()

        #we need this to link old user ids to
        #new users' personal groups
        #self.record_personal_groups()
        
        self.import_user_logins()
        self.import_tags()
        self.import_marked_tags()

        self.import_threads()
        self.apply_groups_to_threads()

        #model="askbot.posttogroup">
        self.import_posts('question', save_redirects=True)
        self.import_posts('answer')
        self.import_posts('comment')
        self.import_post_revisions()
        self.apply_groups_to_posts()
        self.apply_question_followers()
        self.import_votes()

        self.import_badges()
        self.import_badge_awards()
        self.delete_new_messages()
        #we'll try to ignore importing this
        #model="askbot.activity"

    def setup_run(self):
        """remembers the run information, 
        for the logging purposes
        """
        command = ' '.join(sys.argv)
        run = ImportRun.objects.create(command=command)
        self.run = run

    def read_xml_file(self, filename):
        if not os.path.isfile(filename):
            raise CommandError('File %s does not exist') % filename
        xml = open(filename, 'r').read() 
        self.soup = BeautifulSoup(xml, ['lxml', 'xml'])

    def remember_message_ids(self):
        self.message_ids = list(Message.objects.values_list('id', flat=True))

    def log_action_with_old_id(self, from_object_id, to_object, extra_info=None):
        info = ImportedObjectInfo()
        info.old_id = from_object_id
        info.new_id = to_object.id
        info.model = str(to_object._meta)
        info.run = self.run
        info.extra_info = extra_info or dict()
        info.save()

    def log_action(self, from_object, to_object, extra_info=None):
        self.log_action_with_old_id(from_object.id, to_object, extra_info=extra_info)

    def log_personal_group(self, group):
        info = ImportedObjectInfo()
        info.old_id = group.id
        info.new_id = int(group.name.split('_')[-1])
        info.model = 'personal_group'
        info.run = self.run
        info.save()

    def get_imported_object_id_by_old_id(self, model_class, old_id):
        if old_id is None:
            return None
        try:
            log = ImportedObjectInfo.objects.get(
                                        model=str(model_class._meta),
                                        old_id=old_id,
                                        #run=self.run
                                    )
            return log.new_id
        except ImportedObjectInfo.DoesNotExist:
            return None

    def get_imported_object_by_old_id(self, model_class, old_id):
        new_id = self.get_imported_object_id_by_old_id(model_class, old_id)
        if new_id:
            return model_class.objects.get(id=new_id)
        return None

    def get_group_by_old_id(self, old_id):
        normal_group = self.get_imported_object_by_old_id(AuthGroup, old_id)
        if normal_group:
            return Group.objects.get(group_ptr=normal_group)

        log = ImportedObjectInfo.objects.get(
                                        model='personal_group',
                                        old_id=old_id,
                                        #run=self.run
                                    )
        old_user_id = log.new_id
        new_user = self.get_imported_object_by_old_id(User, old_user_id)
        return new_user.get_personal_group()

    def get_objects_for_model(self, model_name):
        """returns iterator of objects from the django
        xml dump by name"""
        object_soup = self.soup.find_all('object', {'model': model_name})
        for datum in object_soup:
            yield get_deserialized_object(datum)

    def read_content_types(self):
        """reads content types from the data dump and makes
        dictionary with keys of old content type ids and
        values - active content type objects"""
        ctypes_map = dict()
        for old_ctype in self.get_objects_for_model('contenttypes.contenttype'):
            ctypes_map[old_ctype.id] = ContentType.objects.get(
                                        app_label=old_ctype.app_label,
                                        model=old_ctype.model
                                    )
        self.content_types_map = ctypes_map
        """
        <object pk="38" model="contenttypes.contenttype">
            <field type="CharField" name="name">activity</field>
            <field type="CharField" name="app_label">askbot</field>
            <field type="CharField" name="model">activity</field>
        </object>
        """

    def get_content_type_by_old_id(self, old_ctype_id):
        return self.content_types_map[old_ctype_id]

    def open_unique_file(self, name_hint):
        """return a file using name_hint as the hint
        for the file name, if file with that name exists,
        create a unique file name containing hint as part of
        the name"""
        if os.path.exists(name_hint):
            info = mkstemp(dir=os.getcwd(), prefix=name_hint + '_')
            name_hint = info[1]
        print 'saving file: %s' % name_hint
        return open(name_hint, 'w')

    def import_groups(self):
        """imports askbot group profiles"""

        #redirects_file = self.open_unique_file('group_redirects')
        
        #1) we import auth groups
        for group in self.get_objects_for_model('auth.group'):

            #old_url = group.get_absolute_url()
            if group.name.startswith('_personal'):
                #we don't import these groups, but log
                #associations between old user ids and old personal
                #group ids, because we create the personal groups
                #anew and so need to have a connection
                #old personal group id --> old user id --> new user id 
                # --> new pers. group id
                self.log_personal_group(group)
                continue
            old_group_id = group.id
            try:
                group = AuthGroup.objects.get(name=group.name)
            except AuthGroup.DoesNotExist:
                group.id = None
                group.save()

            #new_url = group.get_absolute_url()

            #if old_url != new_url:
            #    redirects_file.write('%s %s\n' % (old_url, new_url))

            #we will later populate memberships only in these groups
            self.log_action_with_old_id(old_group_id, group)

        #redirects_file.close()

        #2) we import askbot group profiles only for groups
        for profile in self.get_objects_for_model('askbot.group'):
            auth_group = self.get_imported_object_by_old_id(AuthGroup, profile.group_ptr_id)
            if auth_group is None or auth_group.name.startswith('_personal'):
                continue

            #if profile for this group does not exist, then create new profile and save
            try:
                existing_profile = Group.objects.get(id=auth_group.id)
                copy_string_parameter(profile, existing_profile, 'logo_url')
                merge_words_parameter(profile, existing_profile, 'preapproved_emails')
                merge_words_parameter(profile, existing_profile, 'preapproved_email_domains')
                existing_profile.save()
            except Group.DoesNotExist:
                profile.group_ptr = auth_group
                profile.save()

    def import_users(self):
        redirects_file = self.open_unique_file('user_redirects')

        model_path = str(User._meta)
        dupes = 0
        for from_user in self.get_objects_for_model('auth.user'):
            log_info = dict()
            log_info['notify_user'] = list()

            old_url = from_user.get_absolute_url()

            try:
                to_user = User.objects.get(email=from_user.email)
                dupes += 1
            except User.DoesNotExist:
                username = get_safe_username(from_user.username)
                if username != from_user.username:
                    template = 'Your user name was changed from %s to %s'
                    log_info['notify_user'].append(template % (from_user.username, username))
                to_user = User.objects.create_user(username, from_user.email)

            #copy the data
            if from_user.username != to_user.username:
                names = (from_user.username, to_user.username)
                log_info['notify_user'].append('Your user name has changed from %s to %s' % names)

            copy_string_parameter(from_user, to_user, 'first_name')
            copy_string_parameter(from_user, to_user, 'last_name')
            copy_string_parameter(from_user, to_user, 'real_name')
            copy_string_parameter(from_user, to_user, 'website')
            copy_string_parameter(from_user, to_user, 'location')

            to_user.country = from_user.country

            copy_string_parameter(from_user, to_user, 'about')
            copy_string_parameter(from_user, to_user, 'email_signature')
            copy_string_parameter(from_user, to_user, 'twitter_access_token')
            copy_string_parameter(from_user, to_user, 'twitter_handle')

            merge_words_parameter(from_user, to_user, 'interesting_tags')
            merge_words_parameter(from_user, to_user, 'ignored_tags')
            merge_words_parameter(from_user, to_user, 'subscribed_tags')
            merge_words_parameter(from_user, to_user, 'languages')

            if to_user.password == '!' and from_user.password != '!':
                to_user.password = from_user.password
            copy_bool_parameter(from_user, to_user, 'is_staff')
            copy_bool_parameter(from_user, to_user, 'is_active')
            copy_bool_parameter(from_user, to_user, 'is_superuser')
            copy_bool_parameter(from_user, to_user, 'is_fake', operator='and')
            copy_bool_parameter(from_user, to_user, 'email_isvalid', operator='and')
            copy_bool_parameter(from_user, to_user, 'show_country')
            copy_bool_parameter(from_user, to_user, 'show_marked_tags')

            copy_numeric_parameter(from_user, to_user, 'last_login')
            copy_numeric_parameter(from_user, to_user, 'last_seen')
            copy_numeric_parameter(from_user, to_user, 'date_joined', operator='min')
            copy_numeric_parameter(from_user, to_user, 'email_tag_filter_strategy')
            copy_numeric_parameter(from_user, to_user, 'display_tag_filter_strategy')
            copy_numeric_parameter(
                from_user,
                to_user, 
                'consecutive_days_visit_count',
                operator='sum'
            )
            copy_numeric_parameter(from_user, to_user, 'social_sharing_mode')

            #position of character in this string == rank of status
            if get_status_rank(from_user.status) > get_status_rank(to_user.status):
                to_user.status = from_user.status

            to_user.save()

            new_url = to_user.get_absolute_url()
            if old_url != new_url:
                redirects_file.write('%s %s\n' % (old_url, new_url))

            group_ids = get_m2m_ids_for_field(from_user, 'groups')
            for group_id in group_ids:
                #get group by old id,
                #if group is private - skip,
                #otherwise join this group
                group = self.get_imported_object_by_old_id(Group, int(group_id))
                if group is None or group.name.startswith('_personal'):
                    continue
                #unfortunately, xml dump does not allow us to know of the membership status
                #as m2m user -> group does not contain id of the m2m bridge relation, but
                #only id of the group itself
                to_user.join_group(group, force=True)

            """
            these were not imported:
            <field type="CharField" name="email_key"><None></None></field>
            <field type="PositiveIntegerField" name="reputation">1</field>
            <field type="SmallIntegerField" name="gold">0</field>
            <field type="SmallIntegerField" name="silver">0</field>
            <field type="SmallIntegerField" name="bronze">0</field>
            <field type="IntegerField" name="new_response_count">0</field>
            <field type="IntegerField" name="seen_response_count">0</field>
            """
            self.log_action(from_user, to_user, extra_info=log_info)

    def import_avatars(self):
        """imports user avatar, chooses later uploaded primary avatar"""
        for avatar in self.get_objects_for_model('avatar.avatar'):
            user = self.get_imported_object_by_old_id(User, avatar.user_id)

            if avatar.primary:
                #get other primary avatar and make the later one as primary
                try:
                    existing_avatar = Avatar.objects.get(user=user, primary=True)
                    if existing_avatar.date_uploaded > avatar.date_uploaded:
                        avatar.primary = False
                    else:
                        existing_avatar.primary = False
                        existing_avatar.save()
                except Avatar.DoesNotExist:
                    pass

            avatar.user = user
            avatar.id = None
            avatar.save()
        """
        <object pk="9" model="avatar.avatar">
            <field to="auth.user" name="user" rel="ManyToOneRel">33</field>
            <field type="BooleanField" name="primary">True</field>
            <field type="FileField" name="avatar">avatars/Valdir Barbosa/ValdirBarbosa.png</field>
            <field type="DateTimeField" name="date_uploaded">2013-08-22T16:45:01.517315</field>
        </object>
        """

    def import_marked_tags(self):
        #model="askbot.markedtag">
        for mark in self.get_objects_for_model('askbot.markedtag'):
            tag = self.get_imported_object_by_old_id(Tag, mark.tag_id)
            user = self.get_imported_object_by_old_id(User, mark.user_id)
            user.mark_tags(tagnames=tag.name, reason=mark.reason, action='add')
            """
            <object pk="1" model="askbot.markedtag">
                <field to="askbot.tag" name="tag" rel="ManyToOneRel">13</field>
                <field to="auth.user" name="user" rel="ManyToOneRel">205</field>
                <field type="CharField" name="reason">good</field>
            </object>
            """

    @transaction.commit_manually
    def import_user_logins(self):
        #logins_soup = self.soup.find_all('object', {'model': 'django_authopenid.userassociation'})
        #for login_info in self.get_objects_for_model('django_authopenid.userassociation'):
        #for login_
        for association in self.get_objects_for_model('django_authopenid.userassociation'):
            #where possible, we should copy the login, but respecting the
            #uniqueness constraints: ('user','provider_name'), ('openid_url', 'provider_name')
            #1) get new user by old id
            user = self.get_imported_object_by_old_id(User, association.user_id)
            try:
                association.id = None
                association.user = user
                association.save()
                transaction.commit()
            except:
                transaction.rollback()

    def import_tags(self):
        """imports tag objects"""
        for tag in self.get_objects_for_model('askbot.tag'):
            old_tag_id = tag.id
            try:
                #try to get existing tag with this name
                tag = Tag.objects.get(name__iexact=tag.name)
            except Tag.DoesNotExist:
                tag.id = None
                tag.tag_wiki = None
                tag.created_by = self.get_imported_object_by_old_id(User, tag.created_by_id)
                tag.deleted_by = self.get_imported_object_by_old_id(User, tag.deleted_by_id)
                tag.save()
            self.log_action_with_old_id(old_tag_id, tag)

    def import_threads(self):
        """import thread objects"""
        count = 0
        for thread in self.get_objects_for_model('askbot.thread'):
            count += 1
            new_thread = Thread(
                title=thread.title,
                tagnames=thread.tagnames,
                view_count=thread.view_count,
                favourite_count=thread.favourite_count,
                answer_count=thread.answer_count,
                last_activity_at=thread.last_activity_at,
                last_activity_by=self.get_imported_object_by_old_id(User, thread.last_activity_by_id),
                language_code=thread.language_code,
                closed_by=self.get_imported_object_by_old_id(User, thread.closed_by_id),
                closed=thread.closed,
                closed_at=thread.closed_at,
                close_reason=thread.close_reason,
                deleted=thread.deleted,
                approved=thread.approved,
                answer_accepted_at=thread.answer_accepted_at,
                added_at=thread.added_at,
            )

            #apply tags to threads
            tag_names = thread.get_tag_names()
            if tag_names:

                tag_filter = Q(name__iexact=tag_names[0])
                for tag_name in tag_names[1:]:
                    tag_filter |= Q(name__iexact=tag_name)
                tags = Tag.objects.filter(tag_filter)

                new_thread.tagnames = ' '.join([tag.name for tag in tags])

                new_thread.save()
                for tag in tags:
                    new_thread.tags.add(tag)
                    tag.used_count += 1
                    tag.save()

            else:
                new_thread.save()

            self.log_action(thread, new_thread)
            """
            these are not handled here
            <object pk="155" model="askbot.thread">
                <field to="askbot.post" name="accepted_answer" rel="ManyToOneRel"><None></None></field>
                <field type="IntegerField" name="points">0</field>
                <field to="auth.user" name="followed_by" rel="ManyToManyRel"></field>
            </object>
            """

    def apply_question_followers(self):
        """mark followed questions"""
        for fave in self.get_objects_for_model('askbot.favoritequestion'):
            #askbot.favoritequestion
            user = self.get_imported_object_by_old_id(User, fave.user_id)
            thread = self.get_imported_object_by_old_id(Thread, fave.thread_id)
            user.toggle_favorite_question(thread._question_post(), timestamp=fave.added_at)
            """
            <object pk="1" model="askbot.favoritequestion">
                <field to="askbot.thread" name="thread" rel="ManyToOneRel">8</field>
                <field to="auth.user" name="user" rel="ManyToOneRel">32</field>
                <field type="DateTimeField" name="added_at">2012-12-28T17:34:17.289056</field>
            </object>
            """

    def apply_groups_to_threads(self):
        for link in self.get_objects_for_model('askbot.threadtogroup'):
            thread = self.get_imported_object_by_old_id(Thread, link.thread_id)
            group = self.get_group_by_old_id(link.group_id)
            thread.add_to_groups([group,], visibility=link.visibility)

    def import_posts(self, post_type, save_redirects=False):
        """imports posts of specific post_type"""
        if save_redirects:
            redirects_file = self.open_unique_file('question_redirects')
        for post in self.get_objects_for_model('askbot.post'):
            if post.post_type != post_type:
                continue

            #this line is a bit risky, but should work if we import things in correct order
            post.parent = self.get_imported_object_by_old_id(Post, post.parent_id)

            post.thread = self.get_imported_object_by_old_id(Thread, post.thread_id)

            if save_redirects:
                old_url = post.get_absolute_url(thread=post.thread)

            post.author = self.get_imported_object_by_old_id(User, post.author_id)
            post.deleted_by = self.get_imported_object_by_old_id(User, post.deleted_by_id)
            post.locked_by = self.get_imported_object_by_old_id(User, post.locked_by_id)
            post.last_edited_by = self.get_imported_object_by_old_id(User, post.last_edited_by_id)
            post.points = 0
            post.vote_up_count = 0
            post.vote_down_count = 0
            post.offensive_flag_count = 0

            old_post_id = post.id
            post.id = None
            post.save()

            if save_redirects:
                new_url = post.get_absolute_url()
                if old_url != new_url:
                    redirects_file.write('%s %s\n' % (old_url, new_url))

            self.log_action_with_old_id(old_post_id, post)

        if save_redirects:
            redirects_file.close()

        """
        these were not imported
        votes
        <field type="PositiveIntegerField" name="comment_count">0</field>
        <field type="SmallIntegerField" name="offensive_flag_count">0</field>
        """

    def apply_groups_to_posts(self):
        for link in self.get_objects_for_model('askbot.posttogroup'):
            post = self.get_imported_object_by_old_id(Post, link.post_id)
            group = self.get_group_by_old_id(link.group_id)
            post.add_to_groups([group,])

    def import_post_revisions(self):
        for revision in self.get_objects_for_model('askbot.postrevision'):
            revision.post = self.get_imported_object_by_old_id(Post, revision.post_id)
            revision.author = self.get_imported_object_by_old_id(User, revision.author_id)
            revision.approved_by = self.get_imported_object_by_old_id(User, revision.approved_by_id)
            revision.id = None
            revision.save()

    def import_badges(self):
        """imports badgedata objects"""
        for badge in self.get_objects_for_model('askbot.badgedata'):
            #here we need to make sure that we don't create duplicate badges
            old_badge_id = badge.id
            try:
                new_badge = BadgeData.objects.get(slug=badge.slug)
            except BadgeData.DoesNotExist:
                new_badge = badge
                new_badge.id = None
                new_badge.awarded_count = 0 #we will re-award this, restart count
                new_badge.save()

            self.log_action_with_old_id(old_badge_id, new_badge)
            """
            <object pk="36" model="askbot.badgedata">
                <field type="SlugField" name="slug">taxonomist</field>
                <field type="PositiveIntegerField" name="awarded_count">9</field>
            </object>
            """

    def import_badge_awards(self):
        for award in self.get_objects_for_model('askbot.award'):
            award.user = self.get_imported_object_by_old_id(User, award.user_id)
            badge = self.get_imported_object_by_old_id(BadgeData, award.badge_id)
            #if multiple or user does not have this badge, then award
            if badge.is_multiple() or (not award.user.has_badge(badge)):
                award.badge = badge
                content_type = self.get_content_type_by_old_id(award.content_type_id)
                obj_class = content_type.model_class()
                award.object_id = self.get_imported_object_id_by_old_id(obj_class, award.object_id)
                award.content_type = content_type
                award.id = None
                award.save()
            """
            <object pk="1" model="askbot.award">
                <field to="auth.user" name="user" rel="ManyToOneRel">2</field>
                <field to="askbot.badgedata" name="badge" rel="ManyToOneRel">10</field>
                <field to="contenttypes.contenttype" name="content_type" rel="ManyToOneRel">30</field>
                <field type="PositiveIntegerField" name="object_id">1</field>
                <field type="DateTimeField" name="awarded_at">2012-10-22T18:09:13.527031</field>
                <field type="BooleanField" name="notified">False</field>
            </object>
            """

    def import_votes(self):
        for vote in self.get_objects_for_model('askbot.vote'):
            post = self.get_imported_object_by_old_id(Post, vote.voted_post_id)
            user = self.get_imported_object_by_old_id(User, vote.user_id)
            if vote.vote == 1:
                user.upvote(post, timestamp=vote.voted_at)
            else:
                user.downvote(post, timestamp=vote.voted_at)
            """
            <object pk="1" model="askbot.vote">
                <field to="auth.user" name="user" rel="ManyToOneRel">8</field>
                <field to="askbot.post" name="voted_post" rel="ManyToOneRel">20</field>
                <field type="SmallIntegerField" name="vote">1</field>
                <field type="DateTimeField" name="voted_at">2012-12-26T19:10:08.334818</field>
            </object>
            """

    def delete_new_messages(self):
        Message.objects.exclude(id__in=self.message_ids).delete()
